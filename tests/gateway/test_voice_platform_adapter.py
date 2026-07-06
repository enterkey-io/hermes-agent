"""Tests for the Vox voice gateway platform adapter."""

from inspect import Parameter, signature

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
