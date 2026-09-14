"""Regression coverage for per-turn gateway delivery provenance.

A Telegram stream's successful final send must be tied to the logical response
that produced it.  Persisted delivery receipts are presentation metadata only:
they never alter provider-bound conversation content.
"""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.stream_consumer import GatewayStreamConsumer
from hermes_state import SessionDB


def test_each_stream_consumer_gets_an_opaque_distinct_response_identity():
    first = GatewayStreamConsumer(adapter=object(), chat_id="chat-1")
    second = GatewayStreamConsumer(adapter=object(), chat_id="chat-1")

    assert re.fullmatch(r"[0-9a-f]{32}", first.response_identity)
    assert re.fullmatch(r"[0-9a-f]{32}", second.response_identity)
    assert first.response_identity != second.response_identity


def test_final_delivery_metadata_uses_single_or_split_platform_identity():
    consumer = GatewayStreamConsumer(adapter=object(), chat_id="chat-1")

    consumer._message_id = "single-message"
    consumer._record_final_platform_message_ids()
    assert consumer.final_delivery_metadata == {
        "response_identity": consumer.response_identity,
        "platform_message_id": "single-message",
    }

    consumer._record_final_platform_message_ids(("continuation-1", "continuation-2"))
    assert consumer.final_delivery_metadata == {
        "response_identity": consumer.response_identity,
        "platform_message_ids": [
            "single-message",
            "continuation-1",
            "continuation-2",
        ],
    }


@pytest.mark.asyncio
async def test_fresh_final_receipt_excludes_deleted_preview_identity():
    adapter = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(success=True, message_id="final-message")),
        delete_message=AsyncMock(return_value=True),
    )
    consumer = GatewayStreamConsumer(adapter=adapter, chat_id="chat-1")
    consumer._message_id = "preview-message"
    consumer._platform_message_ids = ["preview-message"]

    assert await consumer._try_fresh_final("complete response") is True
    assert consumer.final_delivery_metadata == {
        "response_identity": consumer.response_identity,
        "platform_message_id": "final-message",
    }


@pytest.mark.asyncio
async def test_replacement_fallback_receipt_excludes_deleted_preview_identity():
    adapter = SimpleNamespace(
        MAX_MESSAGE_LENGTH=4096,
        send=AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="final-message")
        ),
        delete_message=AsyncMock(return_value=True),
    )
    consumer = GatewayStreamConsumer(adapter=adapter, chat_id="chat-1")
    consumer._message_id = "preview-message"
    consumer._last_sent_text = "stale preview"
    consumer._platform_message_ids = ["preview-message"]

    await consumer._send_fallback_final("complete response")

    adapter.delete_message.assert_awaited_once_with("chat-1", "preview-message")
    assert consumer.final_delivery_metadata == {
        "response_identity": consumer.response_identity,
        "platform_message_id": "final-message",
    }


def test_gateway_delivery_receipt_targets_exact_active_row_across_compaction(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("session-1", source="telegram")
    first_turn = [
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content": "same final text",
            "display_metadata": {"existing": {"keep": True}},
        },
    ]
    db.append_messages_batch("session-1", first_turn)
    archived_first_row_id = first_turn[1]["_row_id"]

    # Micro-compaction archives the predecessor and writes the replacement
    # row id back onto the surviving live message dict.
    db.archive_and_compact("session-1", first_turn)
    active_first_row_id = first_turn[1]["_row_id"]
    assert active_first_row_id != archived_first_row_id

    first_receipt = {"response_identity": "a" * 32, "platform_message_id": "first"}
    second_receipt = {
        "response_identity": "b" * 32,
        "platform_message_ids": ["second", "third"],
    }

    try:
        assert db.record_assistant_delivery(
            "session-1", active_first_row_id, first_receipt
        )
        second_turn = [
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "same final text"},
        ]
        db.append_messages_batch("session-1", second_turn)
        assert db.record_assistant_delivery(
            "session-1", second_turn[1]["_row_id"], second_receipt
        )

        rows = db.get_messages("session-1", include_inactive=True)
        archived = next(row for row in rows if row["id"] == archived_first_row_id)
        active_first = next(row for row in rows if row["id"] == active_first_row_id)
        active_second = next(row for row in rows if row["id"] == second_turn[1]["_row_id"])

        assert archived["display_metadata"] == {"existing": {"keep": True}}
        assert active_first["display_metadata"] == {
            "existing": {"keep": True},
            "gateway_delivery": first_receipt,
        }
        assert active_first["platform_message_id"] == "first"
        assert active_second["display_metadata"] == {
            "gateway_delivery": second_receipt
        }
        assert active_second["platform_message_id"] is None

        # Exact retry is idempotent; a conflicting receipt or archived row is
        # rejected without changing either turn.
        assert db.record_assistant_delivery(
            "session-1", active_first_row_id, first_receipt
        )
        assert not db.record_assistant_delivery(
            "session-1",
            active_first_row_id,
            {"response_identity": "c" * 32, "platform_message_id": "wrong"},
        )
        assert not db.record_assistant_delivery(
            "session-1", archived_first_row_id, first_receipt
        )
    finally:
        db.close()
