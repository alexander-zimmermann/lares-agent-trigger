"""Generator tests: the use-case file, the person declaration and the settings in,
the rendered files out.

`rendering/` holds one declaration that touches every surface and the files it
renders to. A deliberate change to the rendering is reviewed as a diff of
those files: `UPDATE_RENDERING=1 pytest tests/test_generate.py` rewrites them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from lares_agent_trigger.generate import GENERATED_LINE, Rendering, main, render
from lares_agent_trigger.persons import load_persons_file
from lares_agent_trigger.use_cases import load_use_case_file

RENDERING = Path(__file__).parent / "rendering"
USE_CASES = RENDERING / "use-cases.yaml"
PERSONS = RENDERING / "persons.yaml"
SETTINGS = RENDERING / "settings.yaml"
EXPECTED = RENDERING / "expected"


def _render(
    tmp_path: Path,
    use_cases: str | None = None,
    settings: str | None = None,
    persons: str | None = None,
) -> Rendering:
    path = USE_CASES
    if use_cases is not None:
        path = tmp_path / "use-cases.yaml"
        path.write_text(use_cases, encoding="utf-8")
    persons_path = PERSONS
    if persons is not None:
        persons_path = tmp_path / "persons.yaml"
        persons_path.write_text(persons, encoding="utf-8")
    text = settings if settings is not None else SETTINGS.read_text(encoding="utf-8")
    return render(load_use_case_file(path), load_persons_file(persons_path), text)


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


def _env_lines(tmp_path: Path, use_cases: str | None = None) -> list[str]:
    patch = yaml.safe_load(_render(tmp_path, use_cases).env_template)
    text: str = patch["spec"]["template"]["data"]["default.env"]
    return text.splitlines()


def test_the_rendering_matches_the_expected_files(tmp_path: Path) -> None:
    rendered = _render(tmp_path)
    files = {
        "config.yaml": rendered.hermes_config,
        "cron-jobs.yaml": rendered.cron_jobs,
        "client-tools.env": rendered.client_tools,
        "tracing.env": rendered.tracing,
        "env-template.yaml": rendered.env_template,
        "clients-template.yaml": rendered.clients_template,
    }
    if os.environ.get("UPDATE_RENDERING"):
        for name, text in files.items():
            (EXPECTED / name).write_text(text, encoding="utf-8")

    for name, text in files.items():
        assert text == (EXPECTED / name).read_text(encoding="utf-8"), name


def test_the_default_profile_runs_on_its_source_and_falls_back_in_order(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert config["model"] == {"provider": "openai-codex", "default": "gpt-6-sol"}
    assert config["fallback_providers"] == [
        {"provider": "openai-api", "model": "gpt-6-luna"},
        {"provider": "openai-api", "model": "gpt-5.6-terra"},
    ]


def test_a_source_nobody_runs_on_renders_nothing(tmp_path: Path) -> None:
    text = PERSONS.read_text(encoding="utf-8")
    without = text[: text.index("  # Nobody runs on it")]

    assert _render(tmp_path, persons=without) == _render(tmp_path)


def test_each_surface_sees_only_the_servers_its_use_cases_may_hold(tmp_path: Path) -> None:
    toolsets = _config(tmp_path)["platform_toolsets"]

    # The chat keeps the harness's own Discord tools and gains the read, request and GitHub servers.
    assert toolsets["discord"] == ["hermes-discord", "lares", "lares-control", "github"]
    # An event run reads, nothing else.
    assert toolsets["api_server"] == ["lares"]
    # Every managed cron job sees this, the Jobs API taking no list per job:
    # every read server in use, and the memory the schedule use cases keep.
    assert toolsets["cron"] == ["hermes-cron", "lares", "lares-memory", "github"]


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


def test_the_github_server_signs_in_as_its_app_and_only_reads(tmp_path: Path) -> None:
    """It mints and renews its own tokens from the App's key; the flags keep it read-only."""
    github = _config(tmp_path)["mcp_servers"]["github"]

    assert github == {
        "command": "/opt/github-mcp/github-mcp-server",
        "args": ["stdio", "--read-only", "--toolsets=repos,issues,pull_requests"],
        "env": {
            "GITHUB_APP_ID": "1234567",
            "GITHUB_APP_INSTALLATION_ID": "89012345",
            "GITHUB_APP_PRIVATE_KEY_PATH": "/etc/github-reader/private-key",
        },
        "timeout": 60,
        "tools": {
            "include": ["get_*", "list_*", "search_*", "issue_read", "pull_request_read"],
            "resources": False,
            "prompts": False,
        },
    }


def test_a_memory_server_reaches_only_the_surface_of_the_use_cases_keeping_one(
    tmp_path: Path,
) -> None:
    """The chat keeps a memory too, but names no memory server: Discord does not get it."""
    toolsets = _config(tmp_path)["platform_toolsets"]

    assert "lares-memory" not in toolsets["discord"]
    assert "lares-memory" not in toolsets["api_server"]
    assert _config(tmp_path)["mcp_servers"]["lares-memory"]["tools"] == {
        "include": ["get_memory", "append_memory"]
    }


def test_the_hook_reports_every_finished_turn_to_the_trigger(tmp_path: Path) -> None:
    assert _config(tmp_path)["hooks"] == {
        "outbound": [
            {
                "name": "lares-agent-trigger",
                "url": "http://lares-agent-trigger.agents.svc.cluster.local:8080/hooks/hermes",
                "events": ["post_api_request", "post_tool_call", "on_session_end"],
                "secret_env": "LARES_AGENT_TRIGGER_HOOK_SECRET",
                "timeout": 10,
            }
        ]
    }


def test_one_sampling_ratio_is_rendered_for_every_service_of_the_platform(
    tmp_path: Path,
) -> None:
    """The trigger, the bridge and the engine read it from the same rendered variable."""
    line = _render(tmp_path).tracing.splitlines()[-1]

    assert line == "TRACING_SAMPLING_RATIO=0.5"
    text = USE_CASES.read_text(encoding="utf-8")
    whole = text.replace("sampling_ratio: 0.5", "sampling_ratio: 1")
    assert _render(tmp_path, whole).tracing.splitlines()[-1] == "TRACING_SAMPLING_RATIO=1.0"


def test_a_schedule_use_case_becomes_one_managed_cron_job(tmp_path: Path) -> None:
    assert _jobs(tmp_path) == [
        {
            "name": "lares:propose-faults",
            "schedule": "0 3 * * 0",
            "skills": ["lares-propose"],
            "prompt": "Answer in English. Stay within 80 tool calls and 20 minutes.",
            # The trigger delivers; the harness keeps the output to itself.
            "deliver": "local",
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
        "lares-memory": ["get_memory", "append_memory"],
    }
    # GitHub's server is no bridge client: the token and the server bound it.


def test_the_default_profiles_env_names_each_key_its_config_reads(tmp_path: Path) -> None:
    """Each value is one sealed key, named after its variable; the template names keys only."""
    assert _env_lines(tmp_path) == [
        # The Runs and Jobs API, and the probes
        """API_SERVER_KEY='{{ index . "api-server-key" }}'""",
        # The chat is on
        """DISCORD_BOT_TOKEN='{{ index . "discord-bot-token" }}'""",
        """LARES_AGENT_TRIGGER_HOOK_SECRET='{{ index . "lares-agent-trigger-hook-secret" }}'""",
        # Each bridge server in use; the dormant lares-wiki and GitHub's App key are not here
        """LARES_MCP_KEY='{{ index . "lares-mcp-key" }}'""",
        """LARES_CONTROL_KEY='{{ index . "lares-control-key" }}'""",
        """LARES_MEMORY_MCP_KEY='{{ index . "lares-memory-mcp-key" }}'""",
        # Both fallbacks run on one API key; the Codex login is on the data volume
        """OPENAI_API_KEY='{{ index . "openai-api-key" }}'""",
    ]


def test_without_a_chat_the_default_profile_holds_no_bot_token(tmp_path: Path) -> None:
    """The chat is what Discord serves; the trigger delivers to Discord with its own mount."""
    text = USE_CASES.read_text(encoding="utf-8")
    without_chat = (
        text[: text.index("  - name: messenger")] + text[text.index("  - name: explain-episode") :]
    )

    assert _env_lines(tmp_path, without_chat) == [
        """API_SERVER_KEY='{{ index . "api-server-key" }}'""",
        """LARES_AGENT_TRIGGER_HOOK_SECRET='{{ index . "lares-agent-trigger-hook-secret" }}'""",
        """LARES_MCP_KEY='{{ index . "lares-mcp-key" }}'""",
        """LARES_MEMORY_MCP_KEY='{{ index . "lares-memory-mcp-key" }}'""",
        """OPENAI_API_KEY='{{ index . "openai-api-key" }}'""",
    ]


def test_the_bridges_clients_file_is_composed_from_one_sealed_key_per_client(
    tmp_path: Path,
) -> None:
    """The bridge's clients of the servers in use, each key sealed under the client's name."""
    patch = yaml.safe_load(_render(tmp_path).clients_template)

    assert patch["metadata"] == {
        "name": "lares-mcp-bridge-auth-clients",
        "namespace": "lares-mcp-bridge",
    }
    assert patch["spec"]["template"]["data"]["clients.json"] == (
        "{\n"
        """  "lares-agent": {{ index . "lares-agent" | toJson }},\n"""
        """  "lares-control": {{ index . "lares-control" | toJson }},\n"""
        """  "lares-memory": {{ index . "lares-memory" | toJson }}\n"""
        "}\n"
    )


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
    # The chat's read servers stay readable for a job made by hand; its request server does not.
    assert toolsets["cron"] == ["hermes-cron", "lares", "github"]
    assert _jobs(tmp_path, only_chat) == []

    only_control = only_chat.replace(
        "    tools: [lares, lares-control, github]\n", "    tools: [lares-control]\n"
    )
    assert _config(tmp_path, only_control)["platform_toolsets"]["cron"] == ["hermes-cron", "no_mcp"]


def test_every_rendered_file_opens_with_the_generated_line(tmp_path: Path) -> None:
    rendered = _render(tmp_path)

    for text in (
        rendered.hermes_config,
        rendered.cron_jobs,
        rendered.client_tools,
        rendered.tracing,
        rendered.env_template,
        rendered.clients_template,
    ):
        assert text.splitlines()[0] == GENERATED_LINE


def test_the_settings_are_kept_whole_beside_the_rendered_part(tmp_path: Path) -> None:
    settings = SETTINGS.read_text(encoding="utf-8")
    rendered = _render(tmp_path).hermes_config
    config = yaml.safe_load(rendered)

    # Comments and all: the settings are copied, never re-serialised.
    assert settings in rendered
    assert {"agent": config["agent"]} == yaml.safe_load(settings)


@pytest.mark.parametrize(
    ("key", "source"),
    [
        ("model", "persons.yaml"),
        ("fallback_providers", "persons.yaml"),
        ("platform_toolsets", "use-cases.yaml"),
        ("hooks", "use-cases.yaml"),
        ("mcp_servers", "use-cases.yaml"),
    ],
)
def test_settings_carrying_a_rendered_key_are_refused(
    tmp_path: Path, key: str, source: str
) -> None:
    settings = SETTINGS.read_text(encoding="utf-8") + f"\n{key}: {{}}\n"

    with pytest.raises(ValueError, match=f"`{key}` is rendered from {source}"):
        _render(tmp_path, settings=settings)


def test_settings_that_are_not_a_mapping_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="a mapping"):
        _render(tmp_path, settings="- model\n")


def _cli(tmp_path: Path, use_cases: Path = USE_CASES, persons: Path = PERSONS) -> int:
    return main(
        [
            "--use-cases",
            str(use_cases),
            "--persons",
            str(persons),
            "--settings",
            str(SETTINGS),
            "--hermes-config",
            str(tmp_path / "out" / "config.yaml"),
            "--cron-jobs",
            str(tmp_path / "out" / "cron-jobs.yaml"),
            "--client-tools",
            str(tmp_path / "out" / "client-tools.env"),
            "--tracing",
            str(tmp_path / "out" / "tracing.env"),
            "--env-template",
            str(tmp_path / "out" / "env-template.yaml"),
            "--clients-template",
            str(tmp_path / "out" / "clients-template.yaml"),
        ]
    )


def test_the_command_writes_every_file_whole(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "config.yaml").write_text("stale\n", encoding="utf-8")

    assert _cli(tmp_path) == 0

    for name in (
        "config.yaml",
        "cron-jobs.yaml",
        "client-tools.env",
        "tracing.env",
        "env-template.yaml",
        "clients-template.yaml",
    ):
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


def test_an_invalid_person_declaration_fails_with_the_field_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "persons.yaml"
    text = PERSONS.read_text(encoding="utf-8")
    broken.write_text(text.replace("    account: per-person\n", "    acount: per-person\n"))
    (tmp_path / "out").mkdir()

    assert _cli(tmp_path, persons=broken) == 1

    error = capsys.readouterr().err
    assert "persons.yaml" in error
    assert "acount" in error
    assert not any((tmp_path / "out").iterdir())
