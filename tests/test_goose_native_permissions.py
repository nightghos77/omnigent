"""Unit tests for the goose-native approval mirror's pane parser."""

from __future__ import annotations

from omnigent.goose_native_permissions import (
    goose_permission_elicitation_id,
    parse_goose_approval_prompt,
)

# cliclack radio with "Always Allow" → Deny is the 3rd item (2 downs from Allow).
_THREE_ITEM = (
    "│ developer__shell\n"
    "│ command: rm -rf /tmp/x\n"
    "◆ Goose would like to call the above tool, do you allow?\n"
    "│ ● Allow          Allow the tool call once\n"
    "│ ○ Always Allow   Always allow the tool call\n"
    "│ ○ Deny           Deny the tool call\n"
    "│ ○ Cancel         Cancel the AI response and tool call\n"
)

# Security-prompt variant: no "Always Allow" → Deny is the 2nd item (1 down).
_TWO_ITEM = (
    "⚠ this command writes files\n"
    "◆ Do you allow this tool call?\n"
    "│ ● Allow   Allow the tool call once\n"
    "│ ○ Deny    Deny the tool call\n"
    "│ ○ Cancel  Cancel the AI response and tool call\n"
)


def test_parses_three_item_prompt_and_deny_index() -> None:
    prompt = parse_goose_approval_prompt(_THREE_ITEM)
    assert prompt is not None
    # Allow(0) → Always Allow(1) → Deny(2): two Down presses.
    assert prompt.deny_down_count == 2
    # Subject is scraped from the tool-request lines above the question.
    assert "developer__shell" in prompt.subject
    assert prompt.block_hash


def test_parses_two_item_prompt_and_deny_index() -> None:
    prompt = parse_goose_approval_prompt(_TWO_ITEM)
    assert prompt is not None
    # Allow(0) → Deny(1): one Down press.
    assert prompt.deny_down_count == 1


def test_requires_question_and_both_items() -> None:
    # Question but no Deny item → not a confirmation block.
    assert parse_goose_approval_prompt("◆ do you allow?\n│ ● Allow\n") is None
    # Items but no question → not live.
    assert parse_goose_approval_prompt("│ ● Allow\n│ ○ Deny\n") is None
    assert parse_goose_approval_prompt("") is None


def test_block_hash_differs_per_tool_and_id_is_deterministic() -> None:
    a = parse_goose_approval_prompt(_THREE_ITEM)
    other = _THREE_ITEM.replace("rm -rf /tmp/x", "cat /etc/passwd")
    b = parse_goose_approval_prompt(other)
    assert a is not None and b is not None
    assert a.block_hash != b.block_hash
    eid = goose_permission_elicitation_id("conv_9", a.block_hash)
    assert eid == goose_permission_elicitation_id("conv_9", a.block_hash)
    assert eid.startswith("elicit_goose_conv_9_")
