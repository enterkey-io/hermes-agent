from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gateway import status
from gateway.config import PlatformConfig
from gateway.platforms.bluebubbles import BlueBubblesAdapter
from tools.send_message_tool import _send_bluebubbles


@pytest.mark.asyncio
async def test_standalone_send_uses_outbound_only_connection():
    calls = []

    class FakeAdapter:
        def __init__(self, _config, *, persist_runtime_status=True):
            assert persist_runtime_status is False

        async def connect(self, *, outbound_only=False):
            calls.append(("connect", outbound_only))
            return True

        async def send(self, chat_id, message):
            calls.append(("send", chat_id, message))
            return SimpleNamespace(success=True, message_id="message-id")

        async def disconnect(self):
            calls.append(("disconnect",))

    with patch(
        "gateway.platforms.bluebubbles.BlueBubblesAdapter", FakeAdapter
    ):
        result = await _send_bluebubbles(
            {"server_url": "http://example.invalid", "password": "secret"},
            "iMessage;+;recipient",
            "hello",
        )

    assert result["success"] is True
    assert calls == [
        ("connect", True),
        ("send", "iMessage;+;recipient", "hello"),
        ("disconnect",),
    ]


@pytest.mark.asyncio
async def test_standalone_send_does_not_overwrite_live_gateway_runtime_status():
    class RecordingAdapter(BlueBubblesAdapter):
        async def connect(self, *, outbound_only=False):
            self._mark_connected()
            return True

        async def send(self, chat_id, message):
            return SimpleNamespace(success=True, message_id="message-id")

        async def disconnect(self):
            self._mark_disconnected()

    with (
        patch("gateway.platforms.bluebubbles.BlueBubblesAdapter", RecordingAdapter),
        patch.object(status, "write_runtime_status") as write_runtime_status,
    ):
        result = await _send_bluebubbles(
            {"server_url": "http://example.invalid", "password": "secret"},
            "iMessage;+;recipient",
            "hello",
        )

    assert result["success"] is True
    write_runtime_status.assert_not_called()


@pytest.mark.asyncio
async def test_outbound_only_disconnect_never_unregisters_gateway_webhook():
    adapter = BlueBubblesAdapter(
        PlatformConfig(
            extra={"server_url": "http://example.invalid", "password": "secret"}
        )
    )
    adapter.client = SimpleNamespace(aclose=AsyncMock())
    adapter._unregister_webhook = AsyncMock(return_value=True)

    await adapter.disconnect()

    adapter._unregister_webhook.assert_not_awaited()
    adapter.client = None


@pytest.mark.asyncio
async def test_send_reconciles_timeout_when_message_is_already_delivered():
    adapter = BlueBubblesAdapter(
        PlatformConfig(
            extra={"server_url": "http://example.invalid", "password": "secret"}
        )
    )
    adapter.client = object()
    adapter._api_post = AsyncMock(
        side_effect=[
            httpx.ReadTimeout("late acknowledgement"),
            {
                "data": [
                    {
                        "guid": "delivered-guid",
                        "text": "hello",
                        "isFromMe": True,
                        "dateCreated": 9_999_999_999_999,
                        "chats": [{"guid": "any;+;recipient"}],
                    }
                ]
            },
        ]
    )

    result = await adapter.send("iMessage;+;recipient", "hello")

    assert result.success is True
    assert result.message_id == "delivered-guid"
    assert adapter._api_post.await_count == 2
