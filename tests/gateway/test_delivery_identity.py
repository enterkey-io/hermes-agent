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


def test_gateway_delivery_metadata_merges_into_the_current_assistant_row(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("session-1", source="telegram")
    db.append_message(
        session_id="session-1",
        role="assistant",
        content="same final text",
        display_metadata={"existing": {"keep": True}},
    )
    first_receipt = {"response_identity": "a" * 32, "platform_message_id": "first"}
    second_receipt = {
        "response_identity": "b" * 32,
        "platform_message_ids": ["second", "third"],
    }

    try:
        assert db.merge_latest_matching_message_display_metadata(
            "session-1",
            role="assistant",
            content="same final text",
            metadata={"gateway_delivery": first_receipt},
        )
        # Simulate micro-compaction's durable-state boundary: a reload must
        # preserve first-turn metadata without allowing it to identify turn two.
        db.close()
        db = SessionDB(db_path=db_path)
        db.append_message(
            session_id="session-1",
            role="assistant",
            content="same final text",
        )
        assert db.merge_latest_matching_message_display_metadata(
            "session-1",
            role="assistant",
            content="same final text",
            metadata={"gateway_delivery": second_receipt},
        )
        messages = db.get_messages_as_conversation("session-1", include_row_ids=True)
        assert messages[0]["display_metadata"] == {
            "existing": {"keep": True},
            "gateway_delivery": first_receipt,
        }
        assert messages[1]["display_metadata"] == {"gateway_delivery": second_receipt}
        assert (
            messages[0]["display_metadata"]["gateway_delivery"]["response_identity"]
            != messages[1]["display_metadata"]["gateway_delivery"]["response_identity"]
        )
    finally:
        db.close()
