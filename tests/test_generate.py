"""Generator tests: the use-case file, the person declaration and the settings in,
the rendered files out.

`rendering/` holds one declaration that touches every surface and the files it
renders to. A deliberate change to the rendering is reviewed as a diff of
those files: `UPDATE_RENDERING=1 pytest tests/test_generate.py` rewrites them.
The expected files pin every rendered value; the tests beside them cover what
those files cannot show: a declaration that differs, and a refusal.
"""

from __future__ import annotations

import os
import re
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
# Named after where it goes, so it is not taken for the declaration it renders.
AUTHENTIK_PERSONS = "authentik/persons.yaml"
OPEN_WEBUI_CONNECTIONS = "open-webui/connections.values.yaml"
# The declaration without its persons: the default profile serves the chat.
_PERSONS_TEXT = PERSONS.read_text(encoding="utf-8")
NO_PERSONS = _PERSONS_TEXT[: _PERSONS_TEXT.index("\npersons:\n")]


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
    declared = load_use_case_file(path)
    return render(declared, load_persons_file(persons_path, declared), text)


def _config(
    tmp_path: Path, use_cases: str | None = None, persons: str | None = None
) -> dict[str, Any]:
    rendered = _render(tmp_path, use_cases, persons=persons)
    loaded: dict[str, Any] = yaml.safe_load(rendered.hermes_config)
    return loaded


def _env_lines(
    tmp_path: Path,
    use_cases: str | None = None,
    persons: str | None = None,
    profile: str = "default",
) -> list[str]:
    patch = yaml.safe_load(_render(tmp_path, use_cases, persons=persons).env_template)
    text: str = patch["spec"]["template"]["data"][f"{profile}.env"]
    return text.splitlines()


def _files(rendered: Rendering) -> dict[str, str]:
    """Every rendered file by its path below the expected files."""
    return {
        "config.yaml": rendered.hermes_config,
        "env-template.yaml": rendered.env_template,
        "routes.values.yaml": rendered.routes_values,
        "profiles/kustomization.yaml": rendered.profiles_kustomization,
        "profiles/profiles.values.yaml": rendered.profiles_values,
        **{f"profiles/{handle}/config.yaml": text for handle, text in rendered.profiles.items()},
        AUTHENTIK_PERSONS: rendered.authentik_persons,
        OPEN_WEBUI_CONNECTIONS: rendered.open_webui_connections,
    }


def _tree(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


def test_the_rendering_matches_the_expected_files(tmp_path: Path) -> None:
    files = _files(_render(tmp_path))
    if os.environ.get("UPDATE_RENDERING"):
        for name, text in files.items():
            (EXPECTED / name).parent.mkdir(parents=True, exist_ok=True)
            (EXPECTED / name).write_text(text, encoding="utf-8")

    assert _tree(EXPECTED) == set(files)
    for name, text in files.items():
        assert text == (EXPECTED / name).read_text(encoding="utf-8"), name


def test_every_rendered_file_opens_with_the_generated_line(tmp_path: Path) -> None:
    rendered = _render(tmp_path)

    for text in _files(rendered).values():
        assert text.splitlines()[0] == GENERATED_LINE


def test_a_source_nobody_runs_on_renders_nothing(tmp_path: Path) -> None:
    text = PERSONS.read_text(encoding="utf-8")
    without = text[: text.index("  # Nobody runs on it")] + text[text.index("\n# The owner") :]

    assert _render(tmp_path, persons=without) == _render(tmp_path)


def test_a_dormant_person_renders_nothing_but_their_inactive_account(tmp_path: Path) -> None:
    """max is dormant: the expected blueprint holds their inactive account, nothing else does."""
    without = _PERSONS_TEXT[: _PERSONS_TEXT.index("  - name: max")]

    with_max = _files(_render(tmp_path))
    without_max = _files(_render(tmp_path, persons=without))
    assert with_max.pop(AUTHENTIK_PERSONS) != without_max.pop(AUTHENTIK_PERSONS)
    assert with_max == without_max


def test_a_dormant_use_case_renders_nothing(tmp_path: Path) -> None:
    """The dormant entry is the only one naming lares-wiki: no entry, no route."""
    text = USE_CASES.read_text(encoding="utf-8")
    without = text[: text.index("  - name: basalte-prose")]

    assert _render(tmp_path, without) == _render(tmp_path)
    assert "lares-wiki" not in _config(tmp_path)["mcp_servers"]
    assert "lares-wiki" not in _routes(tmp_path)


def _values(tmp_path: Path, persons: str | None = None) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_render(tmp_path, persons=persons).profiles_values)
    return loaded


def test_without_persons_or_plugins_the_profiles_values_add_nothing(tmp_path: Path) -> None:
    settings = SETTINGS.read_text(encoding="utf-8")
    without = settings[: settings.index("\n# A plugin vendored")] + "\n"
    rendered = _render(tmp_path, settings=without, persons=NO_PERSONS)

    assert yaml.safe_load(rendered.profiles_values) == {
        "deployment": {"volumes": {}, "volumeMounts": {}, "additionalContainers": {}}
    }


def test_every_enabled_plugin_is_the_config_map_of_its_name_in_every_profile(
    tmp_path: Path,
) -> None:
    """A person's message runs in the person's profile, which finds plugins only under its home."""
    rendered = _render(tmp_path)
    deployment = yaml.safe_load(rendered.profiles_values)["deployment"]
    plugin = "hermes-session-reset-policy"
    homes = ["/opt/data", *(f"/opt/data/profiles/{handle}" for handle in rendered.profiles)]

    mounted = {
        mount["mountPath"]: deployment["volumes"][name]
        for name, mount in deployment["volumeMounts"].items()
        if "/plugins/" in mount["mountPath"]
    }
    assert mounted == {
        f"{home}/plugins/{plugin}": {"configMap": {"name": plugin}} for home in homes
    }


def test_without_persons_only_the_default_profile_mounts_the_plugins(tmp_path: Path) -> None:
    deployment = _values(tmp_path, NO_PERSONS)["deployment"]

    assert deployment["volumeMounts"] == {
        "hermes-session-reset-policy": {
            "mountPath": "/opt/data/plugins/hermes-session-reset-policy",
            "readOnly": True,
        }
    }


def test_without_persons_the_profiles_config_map_is_still_generated(tmp_path: Path) -> None:
    """kustomize refuses a kustomization without anything in it."""
    empty = yaml.safe_load(_render(tmp_path, persons=NO_PERSONS).profiles_kustomization)

    assert empty["configMapGenerator"] == [{"name": "hermes-profiles", "files": []}]


def test_every_profile_config_carries_the_settings(tmp_path: Path) -> None:
    """Hermes reads parts of a profile's file raw, so the settings stand in every one of them."""
    settings: dict[str, Any] = yaml.safe_load(SETTINGS.read_text(encoding="utf-8"))
    rendered = _render(tmp_path)

    for text in (rendered.hermes_config, *rendered.profiles.values()):
        config: dict[str, Any] = yaml.safe_load(text)
        for key, value in settings.items():
            if isinstance(value, dict):
                assert {k: config[key][k] for k in value} == value, key
            else:
                assert config[key] == value, key


def test_the_rendered_discord_keys_join_the_settings_own_blocks(tmp_path: Path) -> None:
    """The settings' own keys first, the rendered rights and routes after; a block the
    settings do not hold stands on its own."""
    settings = SETTINGS.read_text(encoding="utf-8")
    parsed: dict[str, Any] = yaml.safe_load(settings)
    config = _config(tmp_path)

    assert list(config["discord"])[: len(parsed["discord"])] == list(parsed["discord"])
    assert config["discord"]["require_mention"] is True
    assert config["discord"]["allow_from"] == ["${env:DISCORD_USER_KIM}", "${env:DISCORD_USER_LOU}"]
    assert config["gateway"]["multiplex_profiles"] is True
    assert [route["profile"] for route in config["gateway"]["profile_routes"]] == ["kim", "lou"]

    without = settings[: settings.index("# One gateway")]
    bare: dict[str, Any] = yaml.safe_load(_render(tmp_path, settings=without).hermes_config)
    assert bare["gateway"] == {"profile_routes": config["gateway"]["profile_routes"]}


def test_without_a_discord_person_the_default_profile_renders_as_without_persons(
    tmp_path: Path,
) -> None:
    without_discord = _PERSONS_TEXT.replace("    discord: admin\n", "").replace(
        "    discord: true\n", ""
    )

    assert (
        _render(tmp_path, persons=without_discord).hermes_config
        == _render(tmp_path, persons=NO_PERSONS).hermes_config
    )


def test_each_surface_sees_only_the_servers_its_use_cases_may_hold(tmp_path: Path) -> None:
    toolsets = _config(tmp_path, persons=NO_PERSONS)["platform_toolsets"]

    # The chat keeps the harness's own Discord tools and gains the read, request and GitHub servers.
    assert toolsets["discord"] == ["hermes-discord", "lares", "lares-control", "github"]
    # An event run reads, nothing else.
    assert toolsets["api_server"] == ["lares"]
    # Every managed cron job sees this, the Jobs API taking no list per job:
    # every read server in use, and the memory the schedule use cases keep.
    assert toolsets["cron"] == ["hermes-cron", "lares", "lares-memory", "github"]


def test_a_memory_server_reaches_only_the_surface_of_the_use_cases_keeping_one(
    tmp_path: Path,
) -> None:
    """The chat keeps a memory too, but names no memory server: Discord does not get it."""
    toolsets = _config(tmp_path, persons=NO_PERSONS)["platform_toolsets"]

    assert "lares-memory" not in toolsets["discord"]
    assert "lares-memory" not in toolsets["api_server"]


def test_a_surface_without_a_use_case_sees_no_tool_server(tmp_path: Path) -> None:
    """An explicit list without a server name would hand that surface every server."""
    text = USE_CASES.read_text(encoding="utf-8")
    only_chat = text[: text.index("  - name: explain-episode")]

    toolsets = _config(tmp_path, only_chat, NO_PERSONS)["platform_toolsets"]
    assert toolsets["api_server"] == ["no_mcp"]
    # The chat's read servers stay readable for a job made by hand; its request server does not.
    assert toolsets["cron"] == ["hermes-cron", "lares", "github"]

    only_control = only_chat.replace(
        "    tools: [lares, lares-control, github]\n", "    tools: [lares-control]\n"
    )
    only_control_toolsets = _config(tmp_path, only_control, NO_PERSONS)["platform_toolsets"]
    assert only_control_toolsets["cron"] == ["hermes-cron", "no_mcp"]


def _gateway(tmp_path: Path, persons: str | None = None, use_cases: str | None = None) -> Any:
    """The gateway's routes, the chart's `config`, as the gateway reads them before expanding
    its variables."""
    return yaml.safe_load(_render(tmp_path, use_cases, persons=persons).routes_values)["config"]


def _routes(
    tmp_path: Path, persons: str | None = None, use_cases: str | None = None
) -> dict[str, dict[str, Any]]:
    config = _gateway(tmp_path, persons, use_cases)
    return {route["name"]: route for route in config["routes"]}


def test_without_persons_the_default_profile_holds_the_chats_servers(tmp_path: Path) -> None:
    """Its one key opens the routes of the chat's servers, github among them."""
    assert _routes(tmp_path, NO_PERSONS)["github"]["policies"]["apiKey"]["keys"] == [
        {"key": "${GATEWAY_KEY}"}
    ]


def test_a_server_outside_the_cluster_is_told_nobody(tmp_path: Path) -> None:
    """A hosted server gets the credential only: no route, no person."""
    text = USE_CASES.read_text(encoding="utf-8").replace(
        "http://github-mcp-server.agents.svc.cluster.local:8082/mcp",
        "https://mcp.example.com/mcp",
    )
    routes = _routes(tmp_path, use_cases=text)

    (target,) = routes["github"]["backends"][0]["mcp"]["targets"]
    assert target["mcp"] == {"host": "https://mcp.example.com/mcp"}
    assert "transformations" not in target["policies"]
    assert "transformations" in routes["lares"]["backends"][0]["mcp"]["targets"][0]["policies"]


def test_the_gateway_waits_each_server_as_long_as_the_harness_does(tmp_path: Path) -> None:
    """A tool server's `timeout_seconds` holds on its route too, a connector's and speech's on
    their own."""
    declared = load_use_case_file(USE_CASES)
    assert declared.speech is not None
    limits = {name: server.timeout_seconds for name, server in declared.tool_servers.items()}
    limits |= {name: connector.timeout_seconds for name, connector in declared.connectors.items()}
    limits["speech"] = declared.speech.timeout_seconds

    for name, route in _routes(tmp_path).items():
        backend = route["backends"][0]
        (target,) = backend["mcp"]["targets"] if "mcp" in backend else [backend["ai"]]
        assert target["policies"]["http"] == {"requestTimeout": f"{limits[name]}s"}, name


def test_a_credential_in_another_header_goes_as_it_is(tmp_path: Path) -> None:
    text = USE_CASES.read_text(encoding="utf-8").replace(
        "      header: Authorization\n", "      header: X-Api-Key\n"
    )
    github = _routes(tmp_path, use_cases=text)["github"]

    assert github["policies"]["backendAuth"]["key"]["location"] == {"header": {"name": "X-Api-Key"}}


def test_a_glob_inside_a_name_becomes_an_anchored_pattern(tmp_path: Path) -> None:
    text = USE_CASES.read_text(encoding="utf-8").replace(
        "[get_memory, append_memory]", '[get_memory, append_memory, fetch_*_runs, "get_?ag"]'
    )
    memory = _routes(tmp_path, use_cases=text)["lares-memory"]

    assert memory["policies"]["mcpAuthorization"]["rules"][2:] == [
        'mcp.tool.name.matches("^fetch_.*_runs$")',
        'mcp.tool.name.matches("^get_.ag$")',
    ]


def test_a_connector_nobody_signs_in_to_has_no_route(tmp_path: Path) -> None:
    persons = _PERSONS_TEXT.replace("    connectors: [claude-ai]\n", "")

    assert "claude-ai" not in _routes(tmp_path, persons)


def test_speech_takes_every_profiles_key_and_each_clients_own(tmp_path: Path) -> None:
    """The machines', each person's by handle, and a client's by its name."""
    speech = _routes(tmp_path)["speech"]

    assert speech["matches"] == [{"path": {"exact": "/v1/audio/transcriptions"}}]
    assert speech["policies"]["apiKey"] == {
        "mode": "strict",
        "keys": [
            {"key": "${GATEWAY_KEY}"},
            {"key": "${KIM_GATEWAY_KEY}", "metadata": {"enduser_id": "kim"}},
            {"key": "${LOU_GATEWAY_KEY}", "metadata": {"enduser_id": "lou"}},
            {"key": "${OPEN_WEBUI_GATEWAY_KEY}", "metadata": {"enduser_id": "open-webui"}},
        ],
    }


def test_a_clients_key_is_its_own_whatever_its_secret_calls_it(tmp_path: Path) -> None:
    """A client's variable is named by the client, so a key named like kim's opens nothing of
    kim's: kim's routes keep kim's key, and the client gets one of its own."""
    text = USE_CASES.read_text(encoding="utf-8").replace(
        "{secret: agentgateway-keys, key: open-webui-gateway-key}",
        "{secret: open-webui-keys, key: kim-gateway-key}",
    )
    values = yaml.safe_load(_render(tmp_path, use_cases=text).routes_values)
    sources = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in values["extraEnv"]}

    assert sources["KIM_GATEWAY_KEY"] == {"name": "hermes-credentials", "key": "kim-gateway-key"}
    assert sources["OPEN_WEBUI_GATEWAY_KEY"] == {
        "name": "open-webui-keys",
        "key": "kim-gateway-key",
    }
    keys = _routes(tmp_path, use_cases=text)["lares"]["policies"]["apiKey"]["keys"]
    assert "${OPEN_WEBUI_GATEWAY_KEY}" not in [entry["key"] for entry in keys]


def test_a_profile_holding_no_server_reads_its_gateway_key_for_speech(tmp_path: Path) -> None:
    """Every chat goes to a person's profile, so the machines hold no server, yet transcribe."""
    text = USE_CASES.read_text(encoding="utf-8")
    only_chat = text[: text.index("  - name: explain-episode")]
    without_speech = (
        only_chat[: only_chat.index("speech:\n")] + only_chat[only_chat.index("scripts:\n") :]
    )
    key = """GATEWAY_KEY='{{ index . "gateway-key" }}'"""

    assert key in _env_lines(tmp_path, only_chat)
    assert key not in _env_lines(tmp_path, without_speech)
    assert "speech" not in _routes(tmp_path, use_cases=without_speech)


def _secret_items(volumes: list[dict[str, Any]], secret: str) -> list[str]:
    volume = next(v for v in volumes if v.get("secret", {}).get("secretName") == secret)
    return [item["key"] for item in volume["secret"]["items"]]


def test_every_key_the_routes_name_is_a_variable_or_file_the_values_provide(
    tmp_path: Path,
) -> None:
    """The routes hold no key: each one is a variable from a sealed key, or a mounted file."""
    values = yaml.safe_load(_render(tmp_path).routes_values)
    variables = {entry["name"] for entry in values["extraEnv"]}
    files = {
        f"{mount['mountPath']}/{key}"
        for mount in values["extraVolumeMounts"]
        for key in _secret_items(values["extraVolumes"], mount["name"])
    }

    routes = yaml.safe_dump(values["config"])
    named = set(re.findall(r"\$\{(\w+)\}", routes))
    read = set(re.findall(r"file: (\S+)", routes))
    # And what lares sets beside them: the cluster's Authentik in a connector's issuer, and the
    # sampling ratio every service of the platform sets for itself.
    assert named == variables | {"AUTHENTIK_URL", "TRACING_SAMPLING_RATIO"}
    assert read == files


def _open_webui_env(tmp_path: Path, persons: str | None = None) -> list[dict[str, Any]]:
    values = yaml.safe_load(_render(tmp_path, persons=persons).open_webui_connections)
    env: list[dict[str, Any]] = values["commonEnvVars"]
    return env


def test_each_connection_uses_the_key_the_persons_profile_reads(tmp_path: Path) -> None:
    """A key is set before the list that composes it, from the key the profile's own .env names."""
    env = _open_webui_env(tmp_path)
    composed = next(e for e in env if e["name"] == "OPENAI_API_KEYS")["value"]
    keys = re.findall(r"\$\((\w+)\)", composed)
    defined = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in env if "valueFrom" in e}
    position = {e["name"]: index for index, e in enumerate(env)}

    for handle, key in zip(["kim", "lou"], keys, strict=True):
        assert position[key] < position["OPENAI_API_KEYS"]
        lines = _env_lines(tmp_path, profile=handle)
        (profile_key,) = [line for line in lines if line.startswith("API_SERVER_KEY=")]
        assert defined[key] == {
            "name": "open-webui-hermes-keys",
            "key": re.findall(r'"([^"]+)"', profile_key)[0],
        }


def test_without_persons_open_webui_connects_nowhere(tmp_path: Path) -> None:
    """Open WebUI falls back to api.openai.com without a connection; the API is off instead."""
    off = [{"name": "ENABLE_OPENAI_API", "value": "false"}]

    assert _open_webui_env(tmp_path, NO_PERSONS) == off


def test_the_default_profiles_env_names_each_key_its_config_reads(tmp_path: Path) -> None:
    """Each value is one sealed key, named after its variable; the template names keys only."""
    assert _env_lines(tmp_path, persons=NO_PERSONS) == [
        # The Runs and Jobs API, and the probes
        """API_SERVER_KEY='{{ index . "api-server-key" }}'""",
        # The chat is on
        """DISCORD_BOT_TOKEN='{{ index . "discord-bot-token" }}'""",
        """LARES_AGENT_TRIGGER_HOOK_SECRET='{{ index . "lares-agent-trigger-hook-secret" }}'""",
        # Its one gateway key, which opens the routes of the servers it holds;
        # no server's own credential is here
        """GATEWAY_KEY='{{ index . "gateway-key" }}'""",
        # Both fallbacks run on one API key; the Codex login is on the data volume
        """OPENAI_API_KEY='{{ index . "openai-api-key" }}'""",
    ]


def test_without_a_chat_the_default_profile_holds_no_bot_token(tmp_path: Path) -> None:
    """The chat is what Discord serves; the trigger delivers to Discord with its own mount."""
    text = USE_CASES.read_text(encoding="utf-8")
    without_chat = (
        text[: text.index("  - name: messenger")] + text[text.index("  - name: explain-episode") :]
    )

    assert _env_lines(tmp_path, without_chat, NO_PERSONS) == [
        """API_SERVER_KEY='{{ index . "api-server-key" }}'""",
        """LARES_AGENT_TRIGGER_HOOK_SECRET='{{ index . "lares-agent-trigger-hook-secret" }}'""",
        """GATEWAY_KEY='{{ index . "gateway-key" }}'""",
        """OPENAI_API_KEY='{{ index . "openai-api-key" }}'""",
    ]


@pytest.mark.parametrize(
    "key",
    [
        "discord:\n  allow_from: []",
        "discord:\n  allow_admin_from: []",
        "discord:\n  group_allow_admin_from: []",
        "discord:\n  user_allowed_commands: []",
        "discord:\n  group_user_allowed_commands: []",
        "gateway:\n  profile_routes: []",
    ],
)
def test_settings_carrying_a_rendered_key_of_a_shared_block_are_refused(
    tmp_path: Path, key: str
) -> None:
    block, _, rest = key.partition(":\n  ")
    name = rest.partition(":")[0]
    settings = SETTINGS.read_text(encoding="utf-8").replace(f"{block}:\n", f"{block}:\n  {rest}\n")

    with pytest.raises(ValueError, match=f"`{block}.{name}` is rendered from persons.yaml"):
        _render(tmp_path, settings=settings)


@pytest.mark.parametrize(
    ("key", "source"),
    [
        ("model", "persons.yaml"),
        ("fallback_providers", "persons.yaml"),
        ("terminal", "persons.yaml"),
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


@pytest.mark.parametrize(
    "plugins",
    [
        "plugins: []",
        "plugins:\n  enabled: hermes-session-reset-policy",
        "plugins:\n  enabled:\n    - 3",
    ],
)
def test_settings_whose_enabled_plugins_are_not_names_are_refused(
    tmp_path: Path, plugins: str
) -> None:
    settings = SETTINGS.read_text(encoding="utf-8")
    settings = settings[: settings.index("plugins:")] + plugins + "\n"

    with pytest.raises(ValueError, match="`plugins.enabled` must be a list of plugin names"):
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
            "--env-template",
            str(tmp_path / "out" / "env-template.yaml"),
            "--routes-values",
            str(tmp_path / "out" / "routes.values.yaml"),
            "--profiles",
            str(tmp_path / "out" / "profiles"),
            "--authentik-persons",
            str(tmp_path / "out" / AUTHENTIK_PERSONS),
            "--open-webui-connections",
            str(tmp_path / "out" / OPEN_WEBUI_CONNECTIONS),
        ]
    )


def _outside_directories(tmp_path: Path) -> None:
    """Authentik's blueprints and Open WebUI's config: directories the generator does not own."""
    for name in (AUTHENTIK_PERSONS, OPEN_WEBUI_CONNECTIONS):
        (tmp_path / "out" / name).parent.mkdir(parents=True)


def test_the_command_writes_every_file_whole(tmp_path: Path) -> None:
    _outside_directories(tmp_path)
    (tmp_path / "out" / "config.yaml").write_text("stale\n", encoding="utf-8")

    assert _cli(tmp_path) == 0

    assert _tree(tmp_path / "out") == _tree(EXPECTED)
    for name in _tree(EXPECTED):
        written = (tmp_path / "out" / name).read_text(encoding="utf-8")
        assert written == (EXPECTED / name).read_text(encoding="utf-8"), name


def test_the_command_removes_the_profile_of_a_person_no_longer_rendered(tmp_path: Path) -> None:
    """max went dormant: their config leaves, and so does their directory."""
    _outside_directories(tmp_path)
    (tmp_path / "out" / "profiles" / "max").mkdir(parents=True)
    (tmp_path / "out" / "profiles" / "max" / "config.yaml").write_text("old\n", encoding="utf-8")

    assert _cli(tmp_path) == 0

    assert not (tmp_path / "out" / "profiles" / "max").exists()


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
