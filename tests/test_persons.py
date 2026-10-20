"""Loader tests: the model sources, the default profile's source, and the refused files."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from lares_agent_trigger.persons import load_persons_file

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


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "persons.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_every_field_of_a_model_source(tmp_path: Path) -> None:
    declared = load_persons_file(_write(tmp_path, VALID))

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
    declared = load_persons_file(_write(tmp_path, VALID))

    assert declared.default_source.name == "owner-subscription"


def test_a_default_profile_on_an_undeclared_source_is_refused(tmp_path: Path) -> None:
    text = VALID.replace("  model: owner-subscription\n", "  model: owner-plan\n", 1)

    with pytest.raises(ValueError, match="default_profile: no model source named owner-plan"):
        load_persons_file(_write(tmp_path, text))


def test_the_default_profiles_source_names_its_bound(tmp_path: Path) -> None:
    text = VALID.replace(
        "    account: shared-with-default\n    bound: subscription\n",
        "    account: shared-with-default\n",
        1,
    )

    with pytest.raises(
        ValueError, match="owner-subscription: a source a profile runs on names its `bound`"
    ):
        load_persons_file(_write(tmp_path, text))


def test_a_source_nobody_runs_on_may_leave_its_bound_open(tmp_path: Path) -> None:
    text = VALID.replace(
        "    account: per-person\n    bound: subscription\n", "    account: per-person\n", 1
    )

    assert load_persons_file(_write(tmp_path, text)).model_sources["free-account"].bound is None


def test_a_metered_step_names_its_price(tmp_path: Path) -> None:
    text = VALID.replace(
        "        price: {input: 0.10, cached_input: 0.01, output: 0.50}\n\n  - name: free-account",
        "\n  - name: free-account",
        1,
    )

    with pytest.raises(ValueError, match="openai-api/gpt-6-luna is capped and names no `price`"):
        load_persons_file(_write(tmp_path, text))


def test_a_subscription_step_names_no_price(tmp_path: Path) -> None:
    text = VALID.replace(
        "    bound: subscription\n    fallback:",
        "    bound: subscription\n    price: {input: 1, cached_input: 1, output: 1}\n    fallback:",
        1,
    )

    with pytest.raises(ValueError, match="openai-codex/gpt-6-sol is a subscription"):
        load_persons_file(_write(tmp_path, text))


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
        load_persons_file(_write(tmp_path, text))


def test_a_fallback_nobody_reaches_may_leave_its_price_open(tmp_path: Path) -> None:
    head, _, tail = VALID.partition("  - name: free-account")
    tail = tail.replace(
        "        bound: capped\n        price: {input: 0.10, cached_input: 0.01, output: 0.50}\n",
        "",
    )

    declared = load_persons_file(_write(tmp_path, f"{head}  - name: free-account{tail}"))

    (fallback,) = declared.model_sources["free-account"].fallback
    assert (fallback.bound, fallback.price) == (None, None)


def test_a_priced_fallback_without_a_bound_is_metered(tmp_path: Path) -> None:
    text = VALID.replace("        bound: capped\n", "", 1)

    (fallback,) = load_persons_file(_write(tmp_path, text)).default_source.fallback
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
        load_persons_file(_write(tmp_path, f"{head}  - name: free-account{tail}"))


def test_two_sources_of_one_name_are_refused(tmp_path: Path) -> None:
    text = VALID.replace("  - name: free-account", "  - name: owner-subscription")

    with pytest.raises(ValueError, match="model source owner-subscription is declared twice"):
        load_persons_file(_write(tmp_path, text))


def test_an_unknown_field_is_refused(tmp_path: Path) -> None:
    text = VALID.replace("    account: per-person\n", "    account: per-person\n    plan: free\n")

    with pytest.raises(ValueError, match="plan"):
        load_persons_file(_write(tmp_path, text))


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="persons.yaml: cannot be read"):
        load_persons_file(tmp_path / "persons.yaml")
