"""Hermes-native tool-approval mirror (TUI → web elicitation).

The native ``hermes`` TUI gates commands it flags as dangerous with an
in-terminal approval prompt (its own ``tools/approval.py`` gate). That prompt
lives only in the TUI; to also surface it in the Omnigent web UI (so a user can
approve from the chat view, not only the embedded terminal), the runner watches
the Hermes pane:

1. poll ``capture-pane`` and detect the approval block — Hermes renders
   ``⚠️  DANGEROUS COMMAND: <cmd>`` followed by a ``Choice [o/s/a/D]:`` prompt
   (the menu is ``[o]nce | [s]ession | [a]lways | [d]eny``); verified against
   Hermes' own i18n catalog (``locales/en.yaml``),
2. POST it to the server's generic ``native-permission-request`` hook, which
   publishes ``response.elicitation_request`` and parks for the web verdict,
3. on the verdict, send the advertised key (``o`` = allow once, ``d`` = deny)
   into the pane,
4. if the prompt instead disappears on its own (answered in the embedded
   terminal), POST ``external_elicitation_resolved`` so the parked web card
   clears.

This does NOT suppress Hermes' own gate — its prompt stays the source of truth
and the fallback if pane detection ever fails (the user can still answer ``o``/
``d`` in the terminal). Mirrors :mod:`omnigent.cursor_native_permissions`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent.hermes_native_bridge import capture_hermes_pane, send_hermes_pane_keys

_logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.3
# The hook parks server-side until a human answers; allow a day so the runner's
# POST never abandons a live prompt.
_POST_TIMEOUT_S = 86400.0

# Hermes' dangerous-command prompt markers (see hermes-agent locales/en.yaml):
#   ⚠️  DANGEROUS COMMAND: <description>
#         [o]nce  |  [s]ession  |  [a]lways  |  [d]eny
#         Choice [o/s/a/D]:
_HEADER_RE = re.compile(r"DANGEROUS COMMAND:\s*(?P<cmd>.*\S)?", re.IGNORECASE)
# The ``Choice [o/s...]:`` line is only present while the prompt is awaiting an
# answer, so it's the liveness signal (the header may linger in scrollback).
_CHOICE_RE = re.compile(r"Choice\s*\[o/s", re.IGNORECASE)
_ACCEPT_KEY = "o"  # [o]nce
_DECLINE_KEY = "d"  # [d]eny


@dataclass(frozen=True)
class HermesApprovalPrompt:
    """A parsed Hermes dangerous-command approval prompt.

    :param command: The flagged command/description, e.g. ``"rm -rf /tmp/x"``.
    :param message: Human-readable card message.
    :param preview: Compact preview for the card (the command).
    :param accept_key: tmux key to approve once (``"o"``).
    :param decline_key: tmux key to deny (``"d"``).
    :param block_hash: Stable hash of the command used to dedupe across polls and
        to mint a stable elicitation id.
    """

    command: str
    message: str
    preview: str
    accept_key: str
    decline_key: str
    block_hash: str


def hermes_permission_elicitation_id(session_id: str, block_hash: str) -> str:
    """Return the deterministic Omnigent elicitation id for a Hermes prompt."""
    return f"elicit_hermes_{session_id}_{block_hash}"


def parse_hermes_approval_prompt(pane: str) -> HermesApprovalPrompt | None:
    """Parse a Hermes dangerous-command approval block from rendered pane text.

    Requires BOTH the ``DANGEROUS COMMAND:`` header and a live ``Choice [o/s…]:``
    prompt line, so a header lingering in scrollback (already answered) is not
    re-detected.

    :param pane: Visible pane text from ``capture-pane -p``.
    :returns: The parsed prompt, or ``None`` when no live prompt is visible.
    """
    if not pane or not _CHOICE_RE.search(pane):
        return None
    command = ""
    for line in pane.splitlines():
        match = _HEADER_RE.search(line)
        if match:
            command = (match.group("cmd") or "").strip()
            # Drop a leading/trailing decorative spaces and any status glyphs.
            command = command.strip()
    # The header must be present (not just the Choice line) to avoid matching a
    # different prompt that happens to advertise o/s keys.
    if not _HEADER_RE.search(pane):
        return None
    preview = command[:1024]
    block_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]
    return HermesApprovalPrompt(
        command=command,
        message="Hermes flagged a dangerous command. Run it?",
        preview=preview,
        accept_key=_ACCEPT_KEY,
        decline_key=_DECLINE_KEY,
        block_hash=block_hash,
    )


async def supervise_hermes_approval_mirror(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    auth: httpx.Auth | None = None,
    poll_interval_s: float = _POLL_INTERVAL_S,
) -> None:
    """Poll the Hermes pane and mirror its approval prompts to web elicitations.

    Runs for the session's lifetime (cancelled on teardown). At most one prompt
    is active at a time: a new block spawns a task that parks on the server and,
    on the web verdict, sends the keystroke; a block that vanishes while still
    parked means the user answered in the TUI, so the parked card is released.

    :param base_url: Server base URL.
    :param headers: Auth/routing headers for the runner's requests.
    :param session_id: Omnigent conversation id.
    :param bridge_dir: The hermes-native bridge dir holding ``tmux.json``.
    :param auth: Optional httpx auth for the runner's requests.
    :param poll_interval_s: Pane poll cadence in seconds.
    """
    active: dict[str, object] | None = None
    timeout = httpx.Timeout(_POST_TIMEOUT_S, connect=10.0)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                pane = await asyncio.to_thread(capture_hermes_pane, bridge_dir)
                prompt = parse_hermes_approval_prompt(pane) if pane else None
                if prompt is not None:
                    if active is None or active["key"] != prompt.block_hash:
                        elicitation_id = hermes_permission_elicitation_id(
                            session_id, prompt.block_hash
                        )
                        task = asyncio.create_task(
                            _run_one_approval(
                                client,
                                session_id=session_id,
                                bridge_dir=bridge_dir,
                                prompt=prompt,
                                elicitation_id=elicitation_id,
                            ),
                            name=f"hermes-approval-{prompt.block_hash}",
                        )
                        active = {
                            "key": prompt.block_hash,
                            "elicitation_id": elicitation_id,
                            "task": task,
                        }
                elif active is not None:
                    task = active["task"]
                    if isinstance(task, asyncio.Task) and not task.done():
                        await _post_external_elicitation_resolved(
                            client, session_id, str(active["elicitation_id"])
                        )
                    active = None
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "hermes approval mirror poll failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)


async def _run_one_approval(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    prompt: HermesApprovalPrompt,
    elicitation_id: str,
) -> None:
    """Park one Hermes prompt on the server and send the verdict keystroke."""
    payload = {
        "elicitation_id": elicitation_id,
        "agent": "Hermes",
        "policy_name": "hermes_native_permission",
        "operation_type": "shell",
        "message": prompt.message,
        "content_preview": prompt.preview,
    }
    try:
        response = await client.post(
            f"/v1/sessions/{session_id}/hooks/native-permission-request",
            json=payload,
        )
    except httpx.HTTPError:
        _logger.exception("hermes permission hook POST failed; session=%s", session_id)
        return
    if response.status_code >= 400:
        _logger.warning(
            "hermes permission hook rejected: status=%s body=%s",
            response.status_code,
            response.text[:512],
        )
        return
    if not response.content:
        # Empty 2xx → resolved elsewhere (TUI answered) or timeout: no keystroke.
        return
    try:
        result = response.json()
    except ValueError:
        _logger.warning("hermes permission hook returned non-JSON: %s", response.text[:512])
        return
    action = result.get("action") if isinstance(result, dict) else None
    key = None
    if action == "accept":
        key = prompt.accept_key
    elif action in {"decline", "cancel"}:
        key = prompt.decline_key
    if key is None:
        return
    try:
        await asyncio.to_thread(send_hermes_pane_keys, bridge_dir, key)
    except RuntimeError:
        _logger.exception(
            "failed to send hermes approval keystroke %r; session=%s", key, session_id
        )


async def _post_external_elicitation_resolved(
    client: httpx.AsyncClient, session_id: str, elicitation_id: str
) -> None:
    """Tell the server the native TUI answered a pending Hermes prompt."""
    try:
        response = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_elicitation_resolved",
                "data": {"elicitation_id": elicitation_id},
            },
            timeout=10.0,
        )
        if response.status_code >= 400:
            _logger.warning(
                "hermes external_elicitation_resolved rejected: status=%s body=%s",
                response.status_code,
                response.text[:512],
            )
    except httpx.HTTPError:
        _logger.exception("hermes external_elicitation_resolved POST failed")
