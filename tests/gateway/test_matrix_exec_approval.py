import types

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from tests.gateway._approval_binding import pending_pair


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["expired", "missing", "newer"])
async def test_prompt_reaction_is_bound_to_its_request(monkeypatch, binding):
    monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@owner:example.org")
    from plugins.platforms.matrix.adapter import MatrixAdapter

    adapter = MatrixAdapter(PlatformConfig(enabled=True, token="tok", extra={"homeserver": "https://matrix.example.org"}))
    adapter._user_id = "@bot:example.org"
    adapter._client = MagicMock()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="$prompt"))
    adapter._send_reaction = AsyncMock(return_value=None)
    older, newer = pending_pair(monkeypatch, "sess-binding")
    request_id = {"expired": "older-request", "missing": None, "newer": "newer-request"}[binding]
    await adapter.send_exec_approval("!room:example.org", "fixture", "sess-binding", request_id=request_id)
    if binding == "expired":
        older.expires_at = 0
    await adapter._on_reaction(types.SimpleNamespace(
        sender="@owner:example.org", event_id="$reaction", room_id="!room:example.org",
        content={"m.relates_to": {"event_id": "$prompt", "key": "✅"}},
    ))
    assert older.result is None
    assert newer.result == ("once" if binding == "newer" else None)


class TestMatrixExecApprovalReactions:


    @pytest.mark.asyncio
    async def test_reaction_resolves_pending_approval(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@liizfq:liizfq.top")
        from plugins.platforms.matrix.adapter import MatrixAdapter, _MatrixApprovalPrompt

        adapter = MatrixAdapter(PlatformConfig(enabled=True, token="tok", extra={"homeserver": "https://matrix.example.org"}))
        # Resolve user_id so _is_self_sender doesn't defensively drop all traffic (#15763).
        adapter._user_id = "@bot:example.org"
        adapter._approval_prompts_by_event["$target"] = _MatrixApprovalPrompt(
            session_key="sess-1", chat_id="!room:example.org", message_id="$target", request_id="req-1"
        )
        adapter._approval_prompt_by_session["sess-1"] = "$target"

        content = {"m.relates_to": {"event_id": "$target", "key": "✅"}}
        event = types.SimpleNamespace(
            sender="@liizfq:liizfq.top",
            event_id="$react1",
            room_id="!room:example.org",
            content=content,
        )

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            await adapter._on_reaction(event)

        mock_resolve.assert_called_once_with("sess-1", "once", request_id="req-1")
        assert "$target" not in adapter._approval_prompts_by_event
        assert "sess-1" not in adapter._approval_prompt_by_session
