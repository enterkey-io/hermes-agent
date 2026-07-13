from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway import status
from gateway.config import PlatformConfig
from plugins.platforms.matrix.adapter import MatrixAdapter
from tools.send_message_tool import _send_matrix_via_adapter


@pytest.mark.asyncio
async def test_standalone_matrix_send_does_not_overwrite_live_gateway_status():
    class RecordingAdapter(MatrixAdapter):
        async def connect(self):
            self._mark_connected()
            return True

        async def send(self, chat_id, content, metadata=None):
            return SimpleNamespace(success=True, message_id="event-id")

        async def disconnect(self):
            self._mark_disconnected()

    with (
        patch("plugins.platforms.matrix.adapter.MatrixAdapter", RecordingAdapter),
        patch.object(status, "write_runtime_status") as write_runtime_status,
    ):
        result = await _send_matrix_via_adapter(
            PlatformConfig(
                token="test-token",
                extra={"homeserver": "https://matrix.example.invalid"},
            ),
            "!room:example.invalid",
            "hello",
        )

    assert result["success"] is True
    write_runtime_status.assert_not_called()

