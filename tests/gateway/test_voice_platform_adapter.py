"""Tests for the Vox voice gateway platform adapter."""

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
