"""Tests for the Vox voice gateway platform adapter."""

from inspect import Parameter, signature

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig


def test_gateway_runner_creates_voice_adapter():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()

    adapter = runner._create_adapter(
        Platform.VOICE,
        PlatformConfig(enabled=True, extra={"vox_url": "ws://localhost:8600/adapter/hermes"}),
    )

    assert adapter is not None
    assert adapter.platform is Platform.VOICE
    assert adapter._vox_url == "ws://localhost:8600/adapter/hermes"
    assert adapter._platform_name == "hermes"


def test_voice_adapter_uses_configured_platform_name():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()

    adapter = runner._create_adapter(
        Platform.VOICE,
        PlatformConfig(
            enabled=True,
            extra={
                "vox_url": "ws://localhost:8600/adapter/xenia",
                "platform_name": "xenia",
            },
        ),
    )

    assert adapter is not None
    assert adapter._vox_url == "ws://localhost:8600/adapter/xenia"
    assert adapter._platform_name == "xenia"


def test_voice_adapter_connect_accepts_gateway_reconnect_kwarg():
    """Voice must honor the same connect signature the gateway calls."""
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.platforms.voice import VoiceAdapter

    sig = signature(VoiceAdapter.connect)
    base_sig = signature(BasePlatformAdapter.connect)

    param = sig.parameters["is_reconnect"]
    base_param = base_sig.parameters["is_reconnect"]

    assert param.kind is Parameter.KEYWORD_ONLY
    assert param.kind is base_param.kind
    assert param.default is False


def _make_voice_adapter():
    from gateway.platforms.voice import VoiceAdapter

    return VoiceAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "vox_url": "ws://localhost:8600/adapter/grace",
                "platform_name": "grace",
            },
        )
    )


@pytest.mark.asyncio
async def test_phone_transcript_carries_verified_owner_context_once(monkeypatch):
    adapter = _make_voice_adapter()
    events = []

    async def handle_message(event):
        events.append(event)
        return "I hear you."

    sent = []

    async def send_to_vox(message):
        sent.append(message)

    adapter.set_message_handler(handle_message)
    monkeypatch.setattr(adapter, "_send_to_vox", send_to_vox)

    await adapter._handle_vox_message(
        {
            "type": "call_start",
            "callId": "vox-grace",
            "agent": "grace",
            "source": "phone",
            "callerID": "+15555550123",
            "callerName": "Elliott",
            "isElliott": True,
            "direction": "inbound",
        }
    )
    await adapter._handle_vox_message(
        {"type": "text", "callId": "vox-grace", "content": "Can you hear me?"}
    )
    await adapter._handle_vox_message(
        {"type": "text", "callId": "vox-grace", "content": "Good."}
    )

    assert len(events) == 2
    assert events[0].source.user_id == "elliott"
    assert events[0].text.startswith(
        "[Phone call context: incoming FaceTime Audio call from Elliott."
    )
    assert "Can you hear me?" in events[0].text
    assert events[1].text == "Good."
    assert sent == [
        {
            "type": "text",
            "content": "I hear you.",
            "callId": "vox-grace",
        },
        {
            "type": "text",
            "content": "I hear you.",
            "callId": "vox-grace",
        },
    ]


@pytest.mark.asyncio
async def test_unverified_inbound_phone_call_is_hung_up(monkeypatch):
    adapter = _make_voice_adapter()
    events = []
    sent = []

    async def handle_message(event):
        events.append(event)
        return None

    async def send_to_vox(message):
        sent.append(message)

    adapter.set_message_handler(handle_message)
    monkeypatch.setattr(adapter, "_send_to_vox", send_to_vox)

    await adapter._handle_vox_message(
        {
            "type": "call_start",
            "callId": "vox-grace",
            "agent": "grace",
            "source": "phone",
            "callerID": "+15555550999",
            "callerName": "Unknown",
            "isElliott": False,
            "direction": "inbound",
        }
    )
    await adapter._handle_vox_message(
        {"type": "text", "callId": "vox-grace", "content": "Queued speech."}
    )

    assert "vox-grace" not in adapter._active_calls
    assert events == []
    assert sent == [{"type": "hangup", "callId": "vox-grace"}]


@pytest.mark.asyncio
async def test_string_false_does_not_verify_inbound_phone_caller(monkeypatch):
    adapter = _make_voice_adapter()
    sent = []

    async def send_to_vox(message):
        sent.append(message)

    monkeypatch.setattr(adapter, "_send_to_vox", send_to_vox)

    await adapter._handle_vox_message(
        {
            "type": "call_start",
            "callId": "vox-malformed-owner",
            "agent": "grace",
            "source": "phone",
            "isElliott": "false",
            "direction": "inbound",
        }
    )

    assert "vox-malformed-owner" not in adapter._active_calls
    assert sent == [{"type": "hangup", "callId": "vox-malformed-owner"}]


@pytest.mark.asyncio
async def test_regular_voice_transcript_remains_plain_text(monkeypatch):
    adapter = _make_voice_adapter()
    events = []

    async def handle_message(event):
        events.append(event)
        return None

    adapter.set_message_handler(handle_message)

    await adapter._handle_vox_message(
        {
            "type": "call_start",
            "callId": "vox-grace",
            "agent": "grace",
            "source": "voice",
        }
    )
    await adapter._handle_vox_message(
        {"type": "text", "callId": "vox-grace", "content": "Hello."}
    )

    assert len(events) == 1
    assert events[0].source.user_id == "elliott"
    assert events[0].text == "Hello."


@pytest.mark.asyncio
async def test_transport_call_id_can_use_a_stable_session_id(monkeypatch):
    adapter = _make_voice_adapter()
    events = []
    sent = []

    async def handle_message(event):
        events.append(event)
        return "I remember."

    async def send_to_vox(message):
        sent.append(message)

    adapter.set_message_handler(handle_message)
    monkeypatch.setattr(adapter, "_send_to_vox", send_to_vox)

    await adapter._handle_vox_message(
        {
            "type": "call_start",
            "callId": "elevenagents-kenzie-turn-1",
            "sessionId": "vox-kenzie-1",
            "agent": "kenzie",
            "source": "voice",
        }
    )
    await adapter._handle_vox_message(
        {
            "type": "text",
            "callId": "elevenagents-kenzie-turn-1",
            "content": "Do you remember?",
        }
    )

    assert events[0].source.chat_id == "voice:vox-kenzie-1"
    assert sent == [
        {
            "type": "text",
            "content": "I remember.",
            "callId": "elevenagents-kenzie-turn-1",
        }
    ]


@pytest.mark.asyncio
async def test_outbound_phone_call_labels_the_recipient(monkeypatch):
    adapter = _make_voice_adapter()
    events = []

    async def handle_message(event):
        events.append(event)
        return None

    adapter.set_message_handler(handle_message)

    await adapter._handle_vox_message(
        {
            "type": "call_start",
            "callId": "vox-grace",
            "agent": "grace",
            "source": "phone",
            "callerName": "Dr. Smith",
            "isElliott": False,
            "direction": "outbound",
        }
    )
    await adapter._handle_vox_message(
        {"type": "text", "callId": "vox-grace", "content": "Hello?"}
    )

    assert len(events) == 1
    assert events[0].source.user_id == "phone-outbound"
    assert events[0].text.startswith(
        "[Phone call context: outgoing FaceTime Audio call to Dr. Smith."
    )
