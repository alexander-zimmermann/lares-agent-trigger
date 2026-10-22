"""Loader tests: the model sources, the default profile's source, the persons, and the
refused files."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from lares_agent_trigger.persons import PersonsFile, load_persons_file
from lares_agent_trigger.use_cases import load_use_case_file

# The messenger holds lares, lares-control and github; claude-ai is the connector.
USE_CASES = Path(__file__).parent / "rendering" / "use-cases.yaml"

VALID = """
default_profile:
  model: owner-subscription

model_sources:
  - name: owner-subscription
    provider: openai-codex
    model: gpt-6-sol
    account: shared-with-default
    bound: subscription
    fallback:
      - provider: openai-api
        model: gpt-6-luna
        bound: capped
        price: {input: 0.10, cached_input: 0.01, output: 0.50}

  - name: free-account
    provider: openai-codex
    model: gpt-6-luna
    account: per-person
    bound: subscription
    fallback:
      - provider: openai-api
        model: gpt-6-luna
        bound: capped
        price: {input: 0.10, cached_input: 0.01, output: 0.50}
"""


# The owner on the default profile's account and admin on Discord, a household
# member on their own free account, and one who is dormant.
PERSONS = """
persons:
  - name: kim
    discord: admin
    tools: [lares, lares-control, github]
    connectors: [claude-ai]
    model: owner-subscription
    keep_answers: true
    enabled: true

  - name: lou
    discord: true
    tools: [lares]
    model: free-account
    enabled: true

  - name: max
    tools: [lares]
    model: free-account
    dormant: Waits for their own account.
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "persons.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _load(tmp_path: Path, text: str, use_cases: Path = USE_CASES) -> PersonsFile:
    return load_persons_file(_write(tmp_path, text), load_use_case_file(use_cases))


def test_every_field_of_a_model_source(tmp_path: Path) -> None:
    declared = _load(tmp_path, VALID)

    source = declared.model_sources["owner-subscription"]
    assert source.provider == "openai-codex"
    assert source.model == "gpt-6-sol"
    assert source.account == "shared-with-default"
    assert source.bound == "subscription"
    assert source.price is None
    (fallback,) = source.fallback
    assert fallback.provider == "openai-api"
    assert fallback.model == "gpt-6-luna"
    assert fallback.bound == "capped"
    assert fallback.price is not None
    # USD per million tokens, read as decimals: a float would round a price.
    assert fallback.price.input == Decimal("0.10")
    assert fallback.price.cached_input == Decimal("0.01")
    assert fallback.price.output == Decimal("0.50")
    assert list(declared.model_sources) == ["owner-subscription", "free-account"]


def test_the_default_profile_names_its_source(tmp_path: Path) -> None:
    declared = _load(tmp_path, VALID)

    assert declared.default_source.name == "owner-subscription"


def test_a_default_profile_on_an_undeclared_source_is_refused(tmp_path: Path) -> None:
    text = VALID.replace("  model: owner-subscription\n", "  model: owner-plan\n", 1)

    with pytest.raises(ValueError, match="default_profile: no model source named owner-plan"):
        _load(tmp_path, text)


def test_the_default_profiles_source_names_its_bound(tmp_path: Path) -> None:
    text = VALID.replace(
        "    account: shared-with-default\n    bound: subscription\n",
        "    account: shared-with-default\n",
        1,
    )

    with pytest.raises(
        ValueError, match="owner-subscription: a source a profile runs on names its `bound`"
    ):
        _load(tmp_path, text)


def test_a_source_nobody_runs_on_may_leave_its_bound_open(tmp_path: Path) -> None:
    text = VALID.replace(
        "    account: per-person\n    bound: subscription\n", "    account: per-person\n", 1
    )

    assert _load(tmp_path, text).model_sources["free-account"].bound is None


def test_a_metered_step_names_its_price(tmp_path: Path) -> None:
    text = VALID.replace(
        "        price: {input: 0.10, cached_input: 0.01, output: 0.50}\n\n  - name: free-account",
        "\n  - name: free-account",
        1,
    )

    with pytest.raises(ValueError, match="openai-api/gpt-6-luna is capped and names no `price`"):
        _load(tmp_path, text)


def test_a_subscription_step_names_no_price(tmp_path: Path) -> None:
    text = VALID.replace(
        "    bound: subscription\n    fallback:",
        "    bound: subscription\n    price: {input: 1, cached_input: 1, output: 1}\n    fallback:",
        1,
    )

    with pytest.raises(ValueError, match="openai-codex/gpt-6-sol is a subscription"):
        _load(tmp_path, text)


def test_a_reachable_fallback_is_priced_or_a_subscription(tmp_path: Path) -> None:
    text = VALID.replace(
        "        bound: capped\n        price: {input: 0.10, cached_input: 0.01, output: 0.50}\n\n"
        "  - name: free-account",
        "\n  - name: free-account",
        1,
    )

    with pytest.raises(
        ValueError, match="owner-subscription: fallback openai-api/gpt-6-luna is neither priced"
    ):
        _load(tmp_path, text)


def test_a_fallback_nobody_reaches_may_leave_its_price_open(tmp_path: Path) -> None:
    head, _, tail = VALID.partition("  - name: free-account")
    tail = tail.replace(
        "        bound: capped\n        price: {input: 0.10, cached_input: 0.01, output: 0.50}\n",
        "",
    )

    declared = _load(tmp_path, f"{head}  - name: free-account{tail}")

    (fallback,) = declared.model_sources["free-account"].fallback
    assert (fallback.bound, fallback.price) == (None, None)


def test_a_priced_fallback_without_a_bound_is_metered(tmp_path: Path) -> None:
    text = VALID.replace("        bound: capped\n", "", 1)

    (fallback,) = _load(tmp_path, text).default_source.fallback
    assert fallback.bound is None
    assert fallback.price is not None


def test_a_fallback_on_a_codex_login_is_refused(tmp_path: Path) -> None:
    head, _, tail = VALID.partition("  - name: free-account")
    tail = tail.replace(
        "      - provider: openai-api\n        model: gpt-6-luna\n        bound: capped\n"
        "        price: {input: 0.10, cached_input: 0.01, output: 0.50}\n",
        "      - provider: openai-codex\n        model: gpt-6-luna\n        bound: subscription\n",
    )

    with pytest.raises(ValueError, match="free-account: a fallback never runs on openai-codex"):
        _load(tmp_path, f"{head}  - name: free-account{tail}")


def test_a_reachable_provider_without_a_known_key_is_refused(tmp_path: Path) -> None:
    """The profile's .env could not name its key, and the harness would run without one."""
    text = VALID.replace("      - provider: openai-api\n", "      - provider: xai\n", 1)

    with pytest.raises(
        ValueError, match="owner-subscription: xai/gpt-6-luna: no key variable is known for xai"
    ):
        _load(tmp_path, text)


def test_a_provider_nobody_runs_on_needs_no_known_key(tmp_path: Path) -> None:
    head, _, tail = VALID.partition("  - name: free-account")
    tail = tail.replace("      - provider: openai-api\n", "      - provider: xai\n")

    declared = _load(tmp_path, f"{head}  - name: free-account{tail}")

    (fallback,) = declared.model_sources["free-account"].fallback
    assert fallback.provider == "xai"


def test_two_sources_of_one_name_are_refused(tmp_path: Path) -> None:
    text = VALID.replace("  - name: free-account", "  - name: owner-subscription")

    with pytest.raises(ValueError, match="model source owner-subscription is declared twice"):
        _load(tmp_path, text)


def test_an_unknown_field_is_refused(tmp_path: Path) -> None:
    text = VALID.replace("    account: per-person\n", "    account: per-person\n    plan: free\n")

    with pytest.raises(ValueError, match="plan"):
        _load(tmp_path, text)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="persons.yaml: cannot be read"):
        load_persons_file(tmp_path / "persons.yaml", load_use_case_file(USE_CASES))


def test_every_field_of_a_person(tmp_path: Path) -> None:
    declared = _load(tmp_path, VALID + PERSONS)

    kim, lou, max_ = declared.persons.values()
    assert (kim.name, kim.discord, kim.tools, kim.model) == (
        "kim",
        "admin",
        ("lares", "lares-control", "github"),
        "owner-subscription",
    )
    assert kim.keep_answers is True
    assert kim.connectors == ("claude-ai",)
    assert (lou.discord, lou.keep_answers, lou.connectors) == (True, False, ())
    assert (max_.discord, max_.dormant) == (None, "Waits for their own account.")
    assert declared.source_of(lou).name == "free-account"


def test_a_dormant_person_is_declared_and_does_not_run(tmp_path: Path) -> None:
    declared = _load(tmp_path, VALID + PERSONS)

    assert list(declared.persons) == ["kim", "lou", "max"]
    assert [person.name for person in declared.enabled_persons] == ["kim", "lou"]


def test_a_declaration_without_persons_declares_none(tmp_path: Path) -> None:
    assert _load(tmp_path, VALID).persons == {}


def test_a_dormant_person_may_name_what_does_not_exist(tmp_path: Path) -> None:
    """It renders nothing: its tools and its source are checked once it is enabled."""
    text = (VALID + PERSONS).replace(
        "    tools: [lares]\n    model: free-account\n    dormant:",
        "    tools: [lares-wiki]\n    connectors: [openai]\n    model: family-plan\n    dormant:",
    )

    assert _load(tmp_path, text).persons["max"].tools == ("lares-wiki",)


def test_two_persons_of_one_handle_are_refused(tmp_path: Path) -> None:
    text = (VALID + PERSONS).replace("  - name: lou", "  - name: kim")

    with pytest.raises(ValueError, match="person kim is declared twice"):
        _load(tmp_path, text)


@pytest.mark.parametrize("handle", ["Kim", "kim.b", "-kim", "kim_b", "k" * 33, "root"])
def test_a_handle_that_is_no_profile_name_is_refused(tmp_path: Path, handle: str) -> None:
    text = (VALID + PERSONS).replace("  - name: lou", f"  - name: {handle}")

    with pytest.raises(ValueError, match="a handle is a Hermes profile name"):
        _load(tmp_path, text)


def test_the_handle_default_is_refused(tmp_path: Path) -> None:
    text = (VALID + PERSONS).replace("  - name: lou", "  - name: default")

    with pytest.raises(ValueError, match="the default profile belongs to the machines"):
        _load(tmp_path, text)


def test_a_connector_nobody_declared_is_refused(tmp_path: Path) -> None:
    text = (VALID + PERSONS).replace("    connectors: [claude-ai]\n", "    connectors: [openai]\n")

    with pytest.raises(ValueError, match="person kim: connectors: no connector named openai"):
        _load(tmp_path, text)


def test_a_tool_the_chat_does_not_hold_is_refused(tmp_path: Path) -> None:
    text = (VALID + PERSONS).replace(
        "    tools: [lares]\n    model: free-account\n    enabled:",
        "    tools: [lares, lares-memory]\n    model: free-account\n    enabled:",
    )

    with pytest.raises(
        ValueError, match="person lou: tools: lares-memory is not one of the messenger use case's"
    ):
        _load(tmp_path, text)


def test_persons_without_a_chat_are_refused(tmp_path: Path) -> None:
    use_cases = tmp_path / "use-cases.yaml"
    use_cases.write_text(
        USE_CASES.read_text(encoding="utf-8").replace(
            "    memory: true\n    enabled: true\n\n  - name: explain-episode",
            "    memory: true\n    dormant: Off for the test.\n\n  - name: explain-episode",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="person kim: no enabled message use case"):
        _load(tmp_path, VALID + PERSONS, use_cases)


def test_a_person_on_an_undeclared_source_is_refused(tmp_path: Path) -> None:
    text = (VALID + PERSONS).replace(
        "    model: free-account\n    enabled:", "    model: free-plan\n    enabled:"
    )

    with pytest.raises(ValueError, match="person lou: model: no model source named free-plan"):
        _load(tmp_path, text)


def test_a_persons_source_names_its_bound(tmp_path: Path) -> None:
    text = (VALID + PERSONS).replace(
        "    account: per-person\n    bound: subscription\n", "    account: per-person\n", 1
    )

    with pytest.raises(
        ValueError, match="free-account: a source a profile runs on names its `bound`"
    ):
        _load(tmp_path, text)


def test_two_persons_beside_the_default_profile_are_refused(tmp_path: Path) -> None:
    text = (VALID + PERSONS).replace(
        "    model: free-account\n    enabled:", "    model: owner-subscription\n    enabled:"
    )

    with pytest.raises(
        ValueError,
        match="persons kim, lou: at most one person runs on a `shared-with-default` source",
    ):
        _load(tmp_path, text)


# A source on Anthropic itself, and one that falls back to it.
CLAUDE_SOURCE = """
  - name: claude-account
    provider: anthropic
    model: claude-fable-5-1
    account: per-person
    bound: capped
    price: {input: 1, cached_input: 1, output: 1}
"""
CLAUDE_FALLBACK = """
  - name: claude-account
    provider: openai-codex
    model: gpt-6-luna
    account: per-person
    bound: subscription
    fallback:
      - provider: anthropic
        model: claude-fable-5-1
        bound: capped
        price: {input: 1, cached_input: 1, output: 1}
"""


@pytest.mark.parametrize("source", [CLAUDE_SOURCE, CLAUDE_FALLBACK])
def test_a_persons_source_on_anthropic_is_refused(tmp_path: Path, source: str) -> None:
    """It would keep the person's login state in the machines' credentials."""
    persons = PERSONS.replace(
        "    model: free-account\n    enabled:", "    model: claude-account\n    enabled:"
    )

    with pytest.raises(
        ValueError, match="person lou: model source claude-account: anthropic/claude-fable-5-1"
    ):
        _load(tmp_path, VALID + source + persons)


def test_discord_persons_without_an_admin_are_refused(tmp_path: Path) -> None:
    text = (VALID + PERSONS).replace("    discord: admin\n", "    discord: true\n")

    with pytest.raises(
        ValueError, match="persons kim, lou are on Discord, and none is `discord: admin`"
    ):
        _load(tmp_path, text)


def test_a_dormant_admin_leaves_the_discord_persons_without_one(tmp_path: Path) -> None:
    text = (VALID + PERSONS).replace(
        "    keep_answers: true\n    enabled: true\n",
        "    keep_answers: true\n    dormant: Away for the test.\n",
    )

    with pytest.raises(ValueError, match="persons lou are on Discord, and none is"):
        _load(tmp_path, text)


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (
            "    enabled: true\n    dormant: Both.\n",
            "exactly one of `enabled` or `dormant`, not both",
        ),
        ("    dormant:\n", "exactly one of `enabled: true` or `dormant: <reason>`"),
        ('    dormant: ""\n', "dormant"),
        ("    enabled: false\n", "switching a person off is `dormant: <reason>`"),
    ],
)
def test_a_person_runs_or_says_why_not(tmp_path: Path, state: str, message: str) -> None:
    text = (VALID + PERSONS).replace(
        "    model: free-account\n    enabled: true\n", f"    model: free-account\n{state}"
    )

    with pytest.raises(ValueError, match=message):
        _load(tmp_path, text)


@pytest.mark.parametrize("field", ["email: kim@example.org", "discord_id: '880499253825388565'"])
def test_an_identity_field_is_refused(tmp_path: Path, field: str) -> None:
    """Git holds the rights; the identity lives in the sign-in provider."""
    text = (VALID + PERSONS).replace(
        "    keep_answers: true\n", f"    keep_answers: true\n    {field}\n"
    )

    with pytest.raises(ValueError, match=field.partition(":")[0]):
        _load(tmp_path, text)
