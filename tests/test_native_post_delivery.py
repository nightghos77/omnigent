"""Tests for the shared native-forwarder POST delivery classifier."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omnigent import _native_post_delivery
from omnigent._native_post_delivery import (
    _DEAD_LETTER_FILE,
    _DEAD_LETTER_MAX_BYTES,
    append_dead_letter,
    post_may_have_been_delivered,
)


@pytest.mark.parametrize(
    "exc,may_have_been_delivered",
    [
        # Server responded with a status — the events route returns 2xx
        # only after the append + consume publish, so any non-2xx means
        # the item was NOT committed. Safe to retry.
        (
            httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "http://test"),
                response=httpx.Response(503),
            ),
            False,
        ),
        # Connection never established / pool never acquired — no request
        # bytes were sent, so the item was not delivered. Safe to retry.
        (httpx.ConnectError("refused", request=httpx.Request("POST", "http://test")), False),
        (
            httpx.ConnectTimeout("slow connect", request=httpx.Request("POST", "http://test")),
            False,
        ),
        (httpx.PoolTimeout("no slot", request=httpx.Request("POST", "http://test")), False),
        # Request was sent and no response was seen — the server may have
        # committed it. Ambiguous: a retry could duplicate.
        (httpx.ReadTimeout("no response", request=httpx.Request("POST", "http://test")), True),
        (httpx.WriteError("write failed", request=httpx.Request("POST", "http://test")), True),
        (
            httpx.RemoteProtocolError("peer closed", request=httpx.Request("POST", "http://test")),
            True,
        ),
    ],
)
def test_post_may_have_been_delivered_classification(
    exc: httpx.HTTPError, may_have_been_delivered: bool
) -> None:
    """
    Classify which POST failures may have reached + committed the server.

    A forwarder must not retry a POST that may already be committed,
    because external conversation items are not deduped server-side, so
    a retry would surface as a duplicate bubble in the web UI.
    A wrong classification means either duplicates (ambiguous error
    marked safe-to-retry) or lost messages (provably-undelivered error
    marked ambiguous and dropped).

    :param exc: HTTP exception raised while posting an AP event.
    :param may_have_been_delivered: Whether the request may have been
        committed despite the error.
    """
    assert post_may_have_been_delivered(exc) is may_have_been_delivered


def test_append_dead_letter_writes_parseable_line(tmp_path: Path) -> None:
    """
    A dropped forward payload is appended as one parseable JSON line.

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    append_dead_letter(
        tmp_path,
        session_id="conv_abc123",
        event_type="external_conversation_item",
        payload={"item_type": "message", "item_data": {"role": "assistant"}},
        reason="permanent HTTP failure after retries",
    )

    dl_path = tmp_path / _DEAD_LETTER_FILE
    lines = dl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["session_id"] == "conv_abc123"
    assert record["event_type"] == "external_conversation_item"
    assert record["reason"] == "permanent HTTP failure after retries"
    assert record["payload"] == {
        "item_type": "message",
        "item_data": {"role": "assistant"},
    }
    assert isinstance(record["ts"], (int, float))


def test_append_dead_letter_respects_size_cap(tmp_path: Path) -> None:
    """
    A file already at/over the cap is not appended to (write-only artifact).

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    dl_path = tmp_path / _DEAD_LETTER_FILE
    # Use a sparse file (truncate) to reach the cap without writing 50 MB.
    with dl_path.open("wb") as fh:
        fh.truncate(_DEAD_LETTER_MAX_BYTES + 1)
    size_before = dl_path.stat().st_size

    # Clear any latched cap warning for this path so the cap branch is exercised.
    _native_post_delivery._dead_letter_capped.discard(str(dl_path))

    append_dead_letter(
        tmp_path,
        session_id="conv_abc123",
        event_type="external_session_usage",
        payload={"context_tokens": 1},
        reason="post failed",
    )

    assert dl_path.stat().st_size == size_before


def test_append_dead_letter_never_raises_on_unwritable_dir() -> None:
    """A bogus / unwritable bridge dir is swallowed, not raised."""
    bogus = Path("/this/path/does/not/exist/and/cannot/be/made\x00")
    # Must return without raising despite the invalid path.
    append_dead_letter(
        bogus,
        session_id="conv_abc123",
        event_type="external_conversation_item",
        payload={"item_type": "message"},
        reason="post failed",
    )
