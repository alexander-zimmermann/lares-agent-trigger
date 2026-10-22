"""Generator tests: the use-case file, the person declaration and the settings in,
the rendered files out.

`rendering/` holds one declaration that touches every surface and the files it
renders to. A deliberate change to the rendering is reviewed as a diff of
those files: `UPDATE_RENDERING=1 pytest tests/test_generate.py` rewrites them.
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
# The declaration without its persons: the default profile serves the chat.
_PERSONS_TEXT = PERSONS.read_text(encoding="utf-8")
NO_PERSONS = _PERSONS_TEXT[: _PERSONS_TEXT.index("\npersons:\n")]


def _write_use_cases(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "use-cases.yaml"
    path.write_text(text, encoding="utf-8")
    return path


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


def _profile(tmp_path: Path, handle: str, persons: str | None = None) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_render(tmp_path, persons=persons).profiles[handle])
    return loaded


def _jobs(tmp_path: Path, use_cases: str | None = None) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = yaml.safe_load(_render(tmp_path, use_cases).cron_jobs)["jobs"]
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
        "cron-jobs.yaml": rendered.cron_jobs,
        "tracing.env": rendered.tracing,
        "env-template.yaml": rendered.env_template,
        "routes.yaml": rendered.routes,
        "routes.values.yaml": rendered.routes_values,
        "profiles/kustomization.yaml": rendered.profiles_kustomization,
        "profiles/profiles.values.yaml": rendered.profiles_values,
        **{f"profiles/{handle}/config.yaml": text for handle, text in rendered.profiles.items()},
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


def test_the_default_profile_runs_on_its_source_and_falls_back_in_order(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert config["model"] == {"provider": "openai-codex", "default": "gpt-6-sol"}
    assert config["fallback_providers"] == [
        {"provider": "openai-api", "model": "gpt-6-luna"},
        {"provider": "openai-api", "model": "gpt-5.6-terra"},
    ]


def test_the_default_profile_works_in_the_harness_home(tmp_path: Path) -> None:
    """The gateway's working directory, where Hermes discovers the context files."""
    assert _config(tmp_path)["terminal"] == {"cwd": "/opt/data"}


def test_a_source_nobody_runs_on_renders_nothing(tmp_path: Path) -> None:
    text = PERSONS.read_text(encoding="utf-8")
    without = text[: text.index("  # Nobody runs on it")] + text[text.index("\n# The owner") :]

    assert _render(tmp_path, persons=without) == _render(tmp_path)


def test_each_enabled_person_gets_a_profile_of_their_own(tmp_path: Path) -> None:
    """max is dormant."""
    assert list(_render(tmp_path).profiles) == ["kim", "lou"]


def test_a_persons_profile_runs_on_their_source_in_their_directory(tmp_path: Path) -> None:
    lou = _profile(tmp_path, "lou")

    assert lou["model"] == {"provider": "openai-codex", "default": "gpt-6-luna"}
    assert lou["fallback_providers"] == [{"provider": "openai-api", "model": "gpt-6-luna"}]
    assert lou["terminal"] == {"cwd": "/opt/data/profiles/lou"}


def test_a_persons_surfaces_hold_their_own_tool_servers(tmp_path: Path) -> None:
    """Discord keeps the harness's own Discord tools; the web chat comes in on the API server."""
    kim, lou = _profile(tmp_path, "kim"), _profile(tmp_path, "lou")

    assert kim["platform_toolsets"] == {
        "discord": ["hermes-discord", "lares", "lares-control", "github"],
        "api_server": ["lares", "lares-control", "github"],
    }
    assert list(kim["mcp_servers"]) == ["lares", "lares-control", "github"]
    assert lou["platform_toolsets"] == {
        "discord": ["hermes-discord", "lares"],
        "api_server": ["lares"],
    }
    # The default profile's entry: one route per server, whoever holds it.
    assert lou["mcp_servers"] == {"lares": _config(tmp_path)["mcp_servers"]["lares"]}


def _values(tmp_path: Path, persons: str | None = None) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_render(tmp_path, persons=persons).profiles_values)
    return loaded


def test_each_persons_files_are_mounted_read_only_into_their_profile(tmp_path: Path) -> None:
    """Their config and .env, and the files every person shares: SOUL.md, AGENTS.md, and the
    marker that keeps the harness from seeding its bundled skills."""
    deployment = _values(tmp_path)["deployment"]

    assert {
        name: deployment["volumes"][name] for name in deployment["volumes"] if "lou" in name
    } == {
        "hermes-lou-env": {
            "secret": {
                "secretName": "hermes-credentials",
                "items": [{"key": "lou.env", "path": ".env"}],
            }
        },
        "hermes-lou-config": {
            "configMap": {
                "name": "hermes-profiles",
                "items": [{"key": "lou", "path": "config.yaml"}],
            }
        },
        "hermes-lou-soul": {
            "configMap": {
                "name": "hermes-config",
                "items": [{"key": "persons-SOUL.md", "path": "SOUL.md"}],
            }
        },
        "hermes-lou-agents": {
            "configMap": {
                "name": "hermes-config",
                "items": [{"key": "AGENTS.md", "path": "AGENTS.md"}],
            }
        },
        "hermes-lou-no-bundled-skills": {
            "configMap": {
                "name": "hermes-config",
                "items": [{"key": ".no-bundled-skills", "path": ".no-bundled-skills"}],
            }
        },
    }
    mounts = {name: mount for name, mount in deployment["volumeMounts"].items() if "lou" in name}
    assert mounts == {
        f"hermes-lou-{part}": {
            "mountPath": f"/opt/data/profiles/lou/{file}",
            "subPath": file,
            "readOnly": True,
        }
        for part, file in (
            ("env", ".env"),
            ("config", "config.yaml"),
            ("soul", "SOUL.md"),
            ("agents", "AGENTS.md"),
            ("no-bundled-skills", ".no-bundled-skills"),
        )
    }
    # max is dormant.
    assert {name.split("-")[1] for name in deployment["volumes"]} == {"kim", "lou"}


def test_each_persons_agent_log_is_streamed_by_a_container_of_its_own(tmp_path: Path) -> None:
    """Hermes writes each profile's log under its home; Loki names the person by the container."""
    containers = _values(tmp_path)["deployment"]["additionalContainers"]

    # max is dormant.
    assert list(containers) == ["agent-log-kim", "agent-log-lou"]
    assert containers["agent-log-lou"] == {
        # lares pins the image every log container runs under this name.
        "image": "agent-log",
        "command": ["tail", "-n", "0", "-F", "/opt/data/profiles/lou/logs/agent.log"],
        "volumeMounts": [{"name": "hermes-data", "mountPath": "/opt/data", "readOnly": True}],
        "securityContext": {
            "runAsUser": 10000,
            "runAsGroup": 10000,
            "runAsNonRoot": True,
            "readOnlyRootFilesystem": True,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
    }


def test_without_persons_the_profiles_values_add_nothing(tmp_path: Path) -> None:
    assert _values(tmp_path, NO_PERSONS) == {
        "deployment": {"volumes": {}, "volumeMounts": {}, "additionalContainers": {}}
    }


def test_each_persons_config_is_one_key_of_the_profiles_config_map(tmp_path: Path) -> None:
    """Its content hash rolls the harness when a profile changes."""
    kustomization = yaml.safe_load(_render(tmp_path).profiles_kustomization)
    empty = yaml.safe_load(_render(tmp_path, persons=NO_PERSONS).profiles_kustomization)

    assert kustomization["configMapGenerator"] == [
        {"name": "hermes-profiles", "files": ["kim=kim/config.yaml", "lou=lou/config.yaml"]}
    ]
    # kustomize refuses a kustomization without anything in it.
    assert empty["configMapGenerator"] == [{"name": "hermes-profiles", "files": []}]


def test_a_persons_turns_are_reported_like_the_default_profiles(tmp_path: Path) -> None:
    assert _profile(tmp_path, "lou")["hooks"] == _config(tmp_path)["hooks"]


def test_a_persons_profile_carries_the_settings_whole(tmp_path: Path) -> None:
    """The onboarding latches among them: the config is read-only, a latch could not be set."""
    settings = SETTINGS.read_text(encoding="utf-8")
    profile = _render(tmp_path).profiles["lou"]

    assert settings in profile
    assert _profile(tmp_path, "lou")["agent"] == yaml.safe_load(settings)["agent"]


def test_each_discord_person_is_routed_to_their_own_profile(tmp_path: Path) -> None:
    """By the sender's id, sealed in the default profile's .env; max is dormant."""
    assert _config(tmp_path)["gateway"]["profile_routes"] == [
        {
            "name": "kim",
            "platform": "discord",
            "user_id": "${env:DISCORD_USER_KIM}",
            "profile": "kim",
        },
        {
            "name": "lou",
            "platform": "discord",
            "user_id": "${env:DISCORD_USER_LOU}",
            "profile": "lou",
        },
    ]


def test_only_the_discord_persons_may_write_and_only_the_admin_runs_every_command(
    tmp_path: Path,
) -> None:
    """A missing id leaves a person ignored, never misrouted to the default profile."""
    discord = _config(tmp_path)["discord"]

    assert discord["allow_from"] == ["${env:DISCORD_USER_KIM}", "${env:DISCORD_USER_LOU}"]
    assert discord["allow_admin_from"] == ["${env:DISCORD_USER_KIM}"]
    assert discord["group_allow_admin_from"] == ["${env:DISCORD_USER_KIM}"]
    assert discord["user_allowed_commands"] == ["reset", "stop", "retry", "undo"]
    assert discord["group_user_allowed_commands"] == ["reset", "stop", "retry", "undo"]


def test_the_rendered_keys_join_the_settings_own_blocks(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert config["gateway"]["multiplex_profiles"] is True
    assert config["discord"]["require_mention"] is True


def test_the_rendered_keys_stand_alone_when_the_settings_have_no_such_block(
    tmp_path: Path,
) -> None:
    settings = SETTINGS.read_text(encoding="utf-8")
    without = settings[: settings.index("# One gateway")]
    config: dict[str, Any] = yaml.safe_load(_render(tmp_path, settings=without).hermes_config)

    assert config["gateway"] == {"profile_routes": _config(tmp_path)["gateway"]["profile_routes"]}
    assert list(config["discord"]) == [
        "allow_from",
        "allow_admin_from",
        "group_allow_admin_from",
        "user_allowed_commands",
        "group_user_allowed_commands",
    ]


def test_once_persons_are_on_discord_the_default_profile_holds_no_chat_server(
    tmp_path: Path,
) -> None:
    """The machines' profile keeps the event and cron surfaces; the chat is the persons'."""
    config = _config(tmp_path)

    assert config["platform_toolsets"]["discord"] == ["hermes-discord", "no_mcp"]
    # Only the chat held the request server.
    assert "lares-control" not in config["mcp_servers"]


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


def test_a_dormant_person_renders_nothing(tmp_path: Path) -> None:
    without = _PERSONS_TEXT[: _PERSONS_TEXT.index("  - name: max")]

    assert _render(tmp_path, persons=without) == _render(tmp_path)


def test_each_surface_sees_only_the_servers_its_use_cases_may_hold(tmp_path: Path) -> None:
    toolsets = _config(tmp_path, persons=NO_PERSONS)["platform_toolsets"]

    # The chat keeps the harness's own Discord tools and gains the read, request and GitHub servers.
    assert toolsets["discord"] == ["hermes-discord", "lares", "lares-control", "github"]
    # An event run reads, nothing else.
    assert toolsets["api_server"] == ["lares"]
    # Every managed cron job sees this, the Jobs API taking no list per job:
    # every read server in use, and the memory the schedule use cases keep.
    assert toolsets["cron"] == ["hermes-cron", "lares", "lares-memory", "github"]


GATEWAY = "http://agentgateway.agents.svc.cluster.local:8080"
GITHUB_TOOLS = ["get_*", "list_*", "search_*", "issue_read", "pull_request_read"]


def test_every_tool_server_entry_is_its_gateway_route_under_the_profiles_key(
    tmp_path: Path,
) -> None:
    """One shape for every server: the route, the profile's key for it, the include list.

    The gateway serves tools only, so the harness asks for no resources or prompts.
    """
    lares = _config(tmp_path)["mcp_servers"]["lares"]

    assert lares == {
        "url": f"{GATEWAY}/mcp/lares",
        "headers": {"Authorization": "Bearer ${env:LARES_GATEWAY_KEY}"},
        "timeout": 60,
        "tools": {"include": LARES_TOOLS, "resources": False, "prompts": False},
    }


def test_a_person_reaches_every_server_on_the_servers_route(tmp_path: Path) -> None:
    """kim's key names kim to the gateway, so kim needs no route of their own."""
    kim = _profile(tmp_path, "kim")["mcp_servers"]

    assert kim["lares"]["url"] == f"{GATEWAY}/mcp/lares"
    assert kim["lares-control"]["url"] == f"{GATEWAY}/mcp/lares-control"
    assert kim["github"] == {
        "url": f"{GATEWAY}/mcp/github",
        "headers": {"Authorization": "Bearer ${env:GITHUB_GATEWAY_KEY}"},
        "timeout": 60,
        "tools": {"include": GITHUB_TOOLS, "resources": False, "prompts": False},
    }


def _gateway(tmp_path: Path, persons: str | None = None, use_cases: str | None = None) -> Any:
    """The gateway's routes file, as the gateway reads it before expanding its variables."""
    return yaml.safe_load(_render(tmp_path, use_cases, persons=persons).routes)


def _routes(
    tmp_path: Path, persons: str | None = None, use_cases: str | None = None
) -> dict[str, dict[str, Any]]:
    config = _gateway(tmp_path, persons, use_cases)
    return {route["name"]: route for route in config["binds"][0]["listeners"][0]["routes"]}


def test_the_gateway_holds_one_route_per_tool_server_and_one_per_connector(
    tmp_path: Path,
) -> None:
    """Every server a profile holds, once, then every connector a person signs in to."""
    assert list(_routes(tmp_path)) == [
        "lares",
        "lares-control",
        "lares-memory",
        "github",
        "claude-ai",
    ]


def _keyed(tmp_path: Path, persons: str | None = None) -> dict[str, dict[str, Any]]:
    """The routes a profile's key opens: every one but the connectors'."""
    return {
        name: route
        for name, route in _routes(tmp_path, persons).items()
        if "apiKey" in route["policies"]
    }


def test_each_route_takes_the_keys_of_the_profiles_holding_it_and_no_other(
    tmp_path: Path,
) -> None:
    """Each key a variable named after its sealed key, the one the profile's .env reads."""
    keys = {
        name: [key["key"] for key in route["policies"]["apiKey"]["keys"]]
        for name, route in _keyed(tmp_path).items()
    }

    assert keys == {
        "lares": ["${LARES_GATEWAY_KEY}", "${KIM_LARES_GATEWAY_KEY}", "${LOU_LARES_GATEWAY_KEY}"],
        "lares-control": ["${KIM_LARES_CONTROL_GATEWAY_KEY}"],
        "lares-memory": ["${LARES_MEMORY_GATEWAY_KEY}"],
        "github": ["${KIM_GITHUB_GATEWAY_KEY}"],
    }
    assert all(
        route["policies"]["apiKey"]["mode"] == "strict" for route in _keyed(tmp_path).values()
    )
    # Without persons, the default profile holds the chat's servers on the same routes.
    assert _routes(tmp_path, NO_PERSONS)["github"]["policies"]["apiKey"]["keys"] == [
        {"key": "${GITHUB_GATEWAY_KEY}"}
    ]


def test_a_persons_key_carries_their_handle(tmp_path: Path) -> None:
    """The gateway names the person behind the key; the machines' key names nobody."""
    lares = _routes(tmp_path)["lares"]["policies"]["apiKey"]["keys"]

    assert lares == [
        {"key": "${LARES_GATEWAY_KEY}"},
        {"key": "${KIM_LARES_GATEWAY_KEY}", "metadata": {"enduser_id": "kim"}},
        {"key": "${LOU_LARES_GATEWAY_KEY}", "metadata": {"enduser_id": "lou"}},
    ]


def test_each_route_names_the_caller_to_its_server(tmp_path: Path) -> None:
    """On the call to the server, over whatever the caller sent: the route, and the key's person."""
    routes = _routes(tmp_path)

    for name in ("lares", "lares-memory", "github"):
        (target,) = routes[name]["backends"][0]["mcp"]["targets"]
        assert target["policies"] == {
            "transformations": {
                "request": {
                    "set": {"X-Client-Id": f'"{name}"', "X-Enduser-Id": "apiKey.enduser_id"}
                }
            }
        }, name


def test_a_server_outside_the_cluster_is_told_nobody(tmp_path: Path) -> None:
    """A hosted server gets the credential only: no route, no person."""
    text = USE_CASES.read_text(encoding="utf-8").replace(
        "http://github-mcp-server.agents.svc.cluster.local:8082/mcp",
        "https://mcp.example.com/mcp",
    )
    routes = _routes(tmp_path, use_cases=text)

    (target,) = routes["github"]["backends"][0]["mcp"]["targets"]
    assert target == {"name": "github", "mcp": {"host": "https://mcp.example.com/mcp"}}
    assert "policies" in routes["lares"]["backends"][0]["mcp"]["targets"][0]


def test_every_bridge_route_calls_the_bridge_with_the_gateways_key(tmp_path: Path) -> None:
    """One key for the bridge, whichever route the call came in on."""
    routes = _routes(tmp_path)

    assert routes["lares"]["matches"] == [{"path": {"exact": "/mcp/lares"}}]
    mcp = routes["lares"]["backends"][0]["mcp"]
    # A session would keep the credential it started with past a renewal.
    assert mcp["statefulMode"] == "stateless"
    assert [(target["name"], target["mcp"]) for target in mcp["targets"]] == [
        ("lares", {"host": "http://lares-mcp-bridge.lares-mcp-bridge.svc.cluster.local:8080/mcp"})
    ]
    key = {
        "key": {
            "value": {"file": "/etc/agentgateway/credentials/agentgateway-keys/lares-mcp-bridge"}
        }
    }
    for name in ("lares", "lares-control", "lares-memory", "claude-ai"):
        assert routes[name]["policies"]["backendAuth"] == key, name


def test_an_http_route_attaches_its_credential_from_the_mounted_secret(tmp_path: Path) -> None:
    github = _routes(tmp_path)["github"]

    assert github["matches"] == [{"path": {"exact": "/mcp/github"}}]
    (target,) = github["backends"][0]["mcp"]["targets"]
    assert target["mcp"] == {"host": "http://github-mcp-server.agents.svc.cluster.local:8082/mcp"}
    # On Authorization, the gateway's default: a bearer token.
    assert github["policies"]["backendAuth"] == {
        "key": {"value": {"file": "/etc/agentgateway/credentials/github-mcp-server-token/token"}}
    }


def test_a_credential_in_another_header_goes_as_it_is(tmp_path: Path) -> None:
    text = USE_CASES.read_text(encoding="utf-8").replace(
        "      header: Authorization\n", "      header: X-Api-Key\n"
    )
    github = _routes(tmp_path, use_cases=text)["github"]

    assert github["policies"]["backendAuth"]["key"]["location"] == {"header": {"name": "X-Api-Key"}}


def test_each_route_serves_only_its_servers_tools(tmp_path: Path) -> None:
    """One allow rule per entry of `tools`: a glob by its prefix, a name by itself."""
    routes = _routes(tmp_path)

    assert routes["github"]["policies"]["mcpAuthorization"] == {
        "rules": [
            'mcp.tool.name.startsWith("get_")',
            'mcp.tool.name.startsWith("list_")',
            'mcp.tool.name.startsWith("search_")',
            'mcp.tool.name == "issue_read"',
            'mcp.tool.name == "pull_request_read"',
        ]
    }


def test_a_glob_inside_a_name_becomes_an_anchored_pattern(tmp_path: Path) -> None:
    text = USE_CASES.read_text(encoding="utf-8").replace(
        "[get_memory, append_memory]", '[get_memory, append_memory, fetch_*_runs, "get_?ag"]'
    )
    memory = _routes(tmp_path, use_cases=text)["lares-memory"]

    assert memory["policies"]["mcpAuthorization"]["rules"][2:] == [
        'mcp.tool.name.matches("^fetch_.*_runs$")',
        'mcp.tool.name.matches("^get_.ag$")',
    ]


def test_the_gateway_traces_every_call_at_the_platforms_ratio(tmp_path: Path) -> None:
    config = _gateway(tmp_path)

    assert config["frontendPolicies"] == {
        "tracing": {
            "host": "alloy-alloy-receiver.alloy.svc.cluster.local:4318",
            "protocol": "http",
            "randomSampling": "0.5",
        }
    }
    # The gateway listens where the harness calls it.
    assert config["binds"][0]["port"] == 8080


def _secret_items(volumes: list[dict[str, Any]], secret: str) -> list[str]:
    volume = next(v for v in volumes if v.get("secret", {}).get("secretName") == secret)
    return [item["key"] for item in volume["secret"]["items"]]


def test_every_key_the_routes_name_is_a_variable_or_file_the_values_provide(
    tmp_path: Path,
) -> None:
    """The routes file holds no key: each one is a variable from a sealed key, or a mounted file."""
    rendered = _render(tmp_path)
    values = yaml.safe_load(rendered.routes_values)
    variables = {entry["name"] for entry in values["extraEnv"]}
    credentials = [mount for mount in values["extraVolumeMounts"] if mount["name"] != "routes"]
    files = {
        f"{mount['mountPath']}/{key}"
        for mount in credentials
        for key in _secret_items(values["extraVolumes"], mount["name"])
    }

    named = set(re.findall(r"\$\{(\w+)\}", rendered.routes))
    read = set(re.findall(r"file: (\S+)", rendered.routes))
    # And the cluster's Authentik in a connector's issuer, which each overlay sets.
    assert named == variables | {"AUTHENTIK_URL"}
    assert read == files


def test_the_gateway_reads_its_keys_and_mounts_its_routes_and_credentials(tmp_path: Path) -> None:
    """Whole directories, never a subPath: kubelet swaps a renewed Secret in, and it reloads.

    A key is an environment variable, read at start, so a resealed one rolls
    the gateway; a renewed token does not.
    """
    values = yaml.safe_load(_render(tmp_path).routes_values)

    def key(variable: str, sealed: str) -> dict[str, Any]:
        reference = {"name": "agentgateway-keys", "key": sealed}
        return {"name": variable, "valueFrom": {"secretKeyRef": reference}}

    def items(*keys: str) -> list[dict[str, str]]:
        return [{"key": k, "path": k} for k in keys]

    credentials = "/etc/agentgateway/credentials"
    assert values == {
        "config": {"config": {"localXdsPath": "/etc/agentgateway/routes/config.yaml"}},
        "podAnnotations": {"secret.reloader.stakater.com/reload": "agentgateway-keys"},
        "extraEnv": [
            key("LARES_GATEWAY_KEY", "lares-gateway-key"),
            key("KIM_LARES_GATEWAY_KEY", "kim-lares-gateway-key"),
            key("LOU_LARES_GATEWAY_KEY", "lou-lares-gateway-key"),
            key("KIM_LARES_CONTROL_GATEWAY_KEY", "kim-lares-control-gateway-key"),
            key("LARES_MEMORY_GATEWAY_KEY", "lares-memory-gateway-key"),
            key("KIM_GITHUB_GATEWAY_KEY", "kim-github-gateway-key"),
        ],
        "extraVolumes": [
            {"name": "routes", "configMap": {"name": "agentgateway-routes"}},
            {
                "name": "agentgateway-keys",
                "secret": {
                    "secretName": "agentgateway-keys",
                    "items": items("lares-mcp-bridge"),
                },
            },
            {
                "name": "github-mcp-server-token",
                "secret": {"secretName": "github-mcp-server-token", "items": items("token")},
            },
        ],
        "extraVolumeMounts": [
            {"name": "routes", "mountPath": "/etc/agentgateway/routes", "readOnly": True},
            {
                "name": "agentgateway-keys",
                "mountPath": f"{credentials}/agentgateway-keys",
                "readOnly": True,
            },
            {
                "name": "github-mcp-server-token",
                "mountPath": f"{credentials}/github-mcp-server-token",
                "readOnly": True,
            },
        ],
    }


def test_a_memory_server_reaches_only_the_surface_of_the_use_cases_keeping_one(
    tmp_path: Path,
) -> None:
    """The chat keeps a memory too, but names no memory server: Discord does not get it."""
    toolsets = _config(tmp_path, persons=NO_PERSONS)["platform_toolsets"]

    assert "lares-memory" not in toolsets["discord"]
    assert "lares-memory" not in toolsets["api_server"]
    assert _config(tmp_path)["mcp_servers"]["lares-memory"]["tools"]["include"] == [
        "get_memory",
        "append_memory",
    ]


def test_the_hook_reports_every_call_failure_and_finished_turn_to_the_trigger(
    tmp_path: Path,
) -> None:
    assert _config(tmp_path)["hooks"] == {
        "outbound": [
            {
                "name": "lares-agent-trigger",
                "url": "http://lares-agent-trigger.agents.svc.cluster.local:8080/hooks/hermes",
                "events": [
                    "post_api_request",
                    "post_tool_call",
                    "api_request_error",
                    "on_session_end",
                ],
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


LARES_TOOLS = [
    "list_*",
    "get_current_knx",
    "get_episode",
    "query_*",
    "correlate_events",
    "search_wiki",
]


def test_the_default_profiles_env_names_each_key_its_config_reads(tmp_path: Path) -> None:
    """Each value is one sealed key, named after its variable; the template names keys only."""
    assert _env_lines(tmp_path, persons=NO_PERSONS) == [
        # The Runs and Jobs API, and the probes
        """API_SERVER_KEY='{{ index . "api-server-key" }}'""",
        # The chat is on
        """DISCORD_BOT_TOKEN='{{ index . "discord-bot-token" }}'""",
        """LARES_AGENT_TRIGGER_HOOK_SECRET='{{ index . "lares-agent-trigger-hook-secret" }}'""",
        # Its gateway key for each server in use; the dormant lares-wiki is not here,
        # and no server's own credential is
        """LARES_GATEWAY_KEY='{{ index . "lares-gateway-key" }}'""",
        """LARES_CONTROL_GATEWAY_KEY='{{ index . "lares-control-gateway-key" }}'""",
        """LARES_MEMORY_GATEWAY_KEY='{{ index . "lares-memory-gateway-key" }}'""",
        """GITHUB_GATEWAY_KEY='{{ index . "github-gateway-key" }}'""",
        # Both fallbacks run on one API key; the Codex login is on the data volume
        """OPENAI_API_KEY='{{ index . "openai-api-key" }}'""",
    ]


def test_once_persons_are_on_discord_the_default_profile_holds_their_ids(
    tmp_path: Path,
) -> None:
    """The bot still answers through the default profile; the chat's servers are the persons'."""
    assert _env_lines(tmp_path) == [
        """API_SERVER_KEY='{{ index . "api-server-key" }}'""",
        """DISCORD_BOT_TOKEN='{{ index . "discord-bot-token" }}'""",
        """DISCORD_USER_KIM='{{ index . "discord-user-kim" }}'""",
        """DISCORD_USER_LOU='{{ index . "discord-user-lou" }}'""",
        """LARES_AGENT_TRIGGER_HOOK_SECRET='{{ index . "lares-agent-trigger-hook-secret" }}'""",
        """LARES_GATEWAY_KEY='{{ index . "lares-gateway-key" }}'""",
        """LARES_MEMORY_GATEWAY_KEY='{{ index . "lares-memory-gateway-key" }}'""",
        """OPENAI_API_KEY='{{ index . "openai-api-key" }}'""",
    ]


def test_each_persons_env_holds_their_own_keys_and_the_shared_hook_secret(tmp_path: Path) -> None:
    """Their own key of the API server, of each route they hold and of the OpenAI project,
    sealed under the handle; the hook secret is the one every profile shares."""
    assert _env_lines(tmp_path, profile="kim") == [
        """API_SERVER_KEY='{{ index . "kim-api-server-key" }}'""",
        """LARES_AGENT_TRIGGER_HOOK_SECRET='{{ index . "lares-agent-trigger-hook-secret" }}'""",
        """LARES_GATEWAY_KEY='{{ index . "kim-lares-gateway-key" }}'""",
        """LARES_CONTROL_GATEWAY_KEY='{{ index . "kim-lares-control-gateway-key" }}'""",
        """GITHUB_GATEWAY_KEY='{{ index . "kim-github-gateway-key" }}'""",
        """OPENAI_API_KEY='{{ index . "kim-openai-api-key" }}'""",
    ]
    assert _env_lines(tmp_path, profile="lou") == [
        """API_SERVER_KEY='{{ index . "lou-api-server-key" }}'""",
        """LARES_AGENT_TRIGGER_HOOK_SECRET='{{ index . "lares-agent-trigger-hook-secret" }}'""",
        """LARES_GATEWAY_KEY='{{ index . "lou-lares-gateway-key" }}'""",
        """OPENAI_API_KEY='{{ index . "lou-openai-api-key" }}'""",
    ]


def test_every_profiles_env_is_one_file_of_the_harness_secret(tmp_path: Path) -> None:
    patch = yaml.safe_load(_render(tmp_path).env_template)

    assert patch["metadata"] == {"name": "hermes-credentials", "namespace": "agents"}
    assert list(patch["spec"]["template"]["data"]) == ["default.env", "kim.env", "lou.env"]


def test_without_a_chat_the_default_profile_holds_no_bot_token(tmp_path: Path) -> None:
    """The chat is what Discord serves; the trigger delivers to Discord with its own mount."""
    text = USE_CASES.read_text(encoding="utf-8")
    without_chat = (
        text[: text.index("  - name: messenger")] + text[text.index("  - name: explain-episode") :]
    )

    assert _env_lines(tmp_path, without_chat, NO_PERSONS) == [
        """API_SERVER_KEY='{{ index . "api-server-key" }}'""",
        """LARES_AGENT_TRIGGER_HOOK_SECRET='{{ index . "lares-agent-trigger-hook-secret" }}'""",
        """LARES_GATEWAY_KEY='{{ index . "lares-gateway-key" }}'""",
        """LARES_MEMORY_GATEWAY_KEY='{{ index . "lares-memory-gateway-key" }}'""",
        """OPENAI_API_KEY='{{ index . "openai-api-key" }}'""",
    ]


def test_a_dormant_use_case_renders_nothing(tmp_path: Path) -> None:
    """The dormant entry is the only one naming lares-wiki: no entry, no job, no route."""
    text = USE_CASES.read_text(encoding="utf-8")
    without = text[: text.index("  - name: basalte-prose")]

    assert _render(tmp_path, without) == _render(tmp_path)
    assert "lares-wiki" not in _config(tmp_path)["mcp_servers"]
    assert "lares-wiki" not in _routes(tmp_path)


def test_a_surface_without_a_use_case_sees_no_tool_server(tmp_path: Path) -> None:
    """An explicit list without a server name would hand that surface every server."""
    text = USE_CASES.read_text(encoding="utf-8")
    only_chat = text[: text.index("  - name: explain-episode")]

    toolsets = _config(tmp_path, only_chat, NO_PERSONS)["platform_toolsets"]
    assert toolsets["api_server"] == ["no_mcp"]
    # The chat's read servers stay readable for a job made by hand; its request server does not.
    assert toolsets["cron"] == ["hermes-cron", "lares", "github"]
    assert _jobs(tmp_path, only_chat) == []

    only_control = only_chat.replace(
        "    tools: [lares, lares-control, github]\n", "    tools: [lares-control]\n"
    )
    only_control_toolsets = _config(tmp_path, only_control, NO_PERSONS)["platform_toolsets"]
    assert only_control_toolsets["cron"] == ["hermes-cron", "no_mcp"]


def test_every_rendered_file_opens_with_the_generated_line(tmp_path: Path) -> None:
    rendered = _render(tmp_path)

    for text in _files(rendered).values():
        assert text.splitlines()[0] == GENERATED_LINE


def test_the_settings_are_kept_whole_beside_the_rendered_part(tmp_path: Path) -> None:
    settings = SETTINGS.read_text(encoding="utf-8")
    rendered = _render(tmp_path).hermes_config.splitlines()

    # Comments and all, in order: the settings are copied, never re-serialised;
    # the rendered keys of a block the settings hold follow the block's own.
    lines = iter(rendered)
    assert all(line in lines for line in settings.splitlines())
    assert _config(tmp_path)["agent"] == yaml.safe_load(settings)["agent"]


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


def test_a_shared_block_in_flow_style_is_refused(tmp_path: Path) -> None:
    """The rendered keys join the block line by line, which a flow mapping has none of."""
    settings = SETTINGS.read_text(encoding="utf-8").replace(
        "discord:\n  # In a channel, only when mentioned.\n  require_mention: true\n",
        "discord: {require_mention: true}\n",
    )

    with pytest.raises(ValueError, match="`discord` is written as a block mapping"):
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
            "--tracing",
            str(tmp_path / "out" / "tracing.env"),
            "--env-template",
            str(tmp_path / "out" / "env-template.yaml"),
            "--routes",
            str(tmp_path / "out" / "routes.yaml"),
            "--routes-values",
            str(tmp_path / "out" / "routes.values.yaml"),
            "--profiles",
            str(tmp_path / "out" / "profiles"),
        ]
    )


def test_the_command_writes_every_file_whole(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "config.yaml").write_text("stale\n", encoding="utf-8")

    assert _cli(tmp_path) == 0

    assert _tree(tmp_path / "out") == _tree(EXPECTED)
    for name in _tree(EXPECTED):
        written = (tmp_path / "out" / name).read_text(encoding="utf-8")
        assert written == (EXPECTED / name).read_text(encoding="utf-8"), name


def test_the_command_removes_the_profile_of_a_person_no_longer_rendered(tmp_path: Path) -> None:
    """max went dormant: their config leaves, and so does their directory."""
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


def test_a_connector_signs_its_persons_in_through_authentik(tmp_path: Path) -> None:
    """Its path and the metadata beside it; Authentik registers no clients, so the gateway hands
    out the pre-registered one."""
    claude = _routes(tmp_path)["claude-ai"]

    assert claude["matches"] == [
        {"path": {"exact": "/mcp"}},
        {"path": {"exact": "/.well-known/oauth-protected-resource/mcp"}},
        {"path": {"exact": "/.well-known/oauth-authorization-server/mcp"}},
        {"path": {"exact": "/.well-known/oauth-authorization-server/mcp/client-registration"}},
    ]
    assert claude["policies"]["mcpAuthentication"] == {
        "issuer": "${AUTHENTIK_URL}/application/o/claude-ai/",
        "audiences": ["claude-ai"],
        "provider": {"authentik": {}},
        "clientId": "claude-ai",
        "resourceMetadata": {
            "scopesSupported": ["openid", "profile", "email", "offline_access"],
            "bearerMethodsSupported": ["header"],
        },
    }


def test_a_connector_serves_its_tools_to_its_persons_only(tmp_path: Path) -> None:
    """Anyone else Authentik signs in is refused; the person is named to the server."""
    claude = _routes(tmp_path)["claude-ai"]

    assert claude["policies"]["mcpAuthorization"] == {"rules": ['mcp.tool.name.startsWith("")']}
    # On the way to the server, after the sign-in has read the token.
    assert claude["backends"][0]["policies"] == {
        "authorization": {"rules": ['jwt.preferred_username in ["kim"]']}
    }
    (target,) = claude["backends"][0]["mcp"]["targets"]
    assert target["policies"]["transformations"]["request"]["set"] == {
        "X-Client-Id": '"claude-ai"',
        "X-Enduser-Id": "jwt.preferred_username",
    }


def test_a_connector_nobody_signs_in_to_has_no_route(tmp_path: Path) -> None:
    persons = _PERSONS_TEXT.replace("    connectors: [claude-ai]\n", "")

    assert "claude-ai" not in _routes(tmp_path, persons)
