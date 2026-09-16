from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter
from tests.gateway._approval_binding import pending_pair


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["expired", "missing", "newer"])
async def test_sent_button_resolves_only_its_request(monkeypatch, binding):
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "42")
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._allowed_user_ids = {"42"}
    sent = _capture_channel(adapter)
    older, newer = pending_pair(monkeypatch, "sess-binding")
    request_id = {"expired": "older-request", "missing": None, "newer": "newer-request"}[binding]
    result = await adapter.send_exec_approval("555", "fixture", "sess-binding", request_id=request_id)
    assert result.success
    if binding == "expired":
        older.expires_at = 0
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=42, display_name="Owner", roles=[]),
        message=SimpleNamespace(embeds=[sent["embed"]]),
        response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
    )
    button = sent["view"].allow_once
    if hasattr(button, "callback"):
        await button.callback(interaction)
    else:
        # The optional-SDK test stub leaves decorated handlers as methods.
        await button(interaction, None)
    assert older.result is None
    assert newer.result == ("once" if binding == "newer" else None)
    interaction.response.edit_message.assert_awaited_once()


def _capture_channel(adapter):
    sent = {}

    async def fake_send(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(id=1234)

    channel = SimpleNamespace(send=AsyncMock(side_effect=fake_send))
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )
    return sent


@pytest.mark.asyncio
async def test_exec_approval_prompt_uses_visible_content_with_command_and_reason():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = _capture_channel(adapter)

    command = "python scripts/deploy.py --env prod --force"
    result = await adapter.send_exec_approval(
        chat_id="555",
        command=command,
        session_key="discord:555",
        description="script execution via -c flag",
    )

    assert result.success is True
    assert sent["view"] is not None
    assert sent["embed"] is not None

    prompt_text = sent["content"]
    assert "Command Approval Required" in prompt_text
    assert "Do you want Hermes to run this command?" in prompt_text
    assert "Requested command" in prompt_text
    assert command in prompt_text
    assert "Reason" in prompt_text
    assert "script execution via -c flag" in prompt_text
