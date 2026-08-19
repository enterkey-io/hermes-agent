from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cron.scheduler import _deliver_result
from gateway.config import Platform, PlatformConfig


ROOMS = {
    "root-update": "11111111-1111-1111-1111-111111111111",
    "emily-saved-items": "22222222-2222-2222-2222-222222222222",
    "ordinary-finance": "33333333-3333-3333-3333-333333333333",
    "failure-path": "44444444-4444-4444-4444-444444444444",
}


@pytest.mark.parametrize("fixture_name", list(ROOMS))
def test_team_delivery_targets_one_buzz_room_exactly_once(fixture_name):
    room_id = ROOMS[fixture_name]
    buzz = AsyncMock()
    buzz.send.return_value = MagicMock(success=True, message_id="event-1")
    telegram = AsyncMock()
    config = MagicMock()
    config.platforms = {Platform("buzz"): PlatformConfig(enabled=True)}
    loop = MagicMock()
    loop.is_running.return_value = True

    def run_coro(coro, _loop):
        import asyncio

        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:  # pragma: no cover - test plumbing
            future.set_exception(exc)
        return future

    standalone = AsyncMock(return_value={"success": True})
    job = {"id": fixture_name, "name": fixture_name, "deliver": f"buzz:{room_id}"}
    content = "FAILED: verified exception" if fixture_name == "failure-path" else "verified result"
    with (
        patch("gateway.config.load_gateway_config", return_value=config),
        patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}),
        patch("asyncio.run_coroutine_threadsafe", side_effect=run_coro),
        patch("tools.send_message_tool._send_to_platform", new=standalone),
    ):
        result = _deliver_result(
            job,
            content,
            adapters={Platform("buzz"): buzz, Platform.TELEGRAM: telegram},
            loop=loop,
        )

    assert result is None
    buzz.send.assert_awaited_once()
    assert buzz.send.await_args.args[:2] == (room_id, content)
    telegram.send.assert_not_awaited()
    standalone.assert_not_awaited()


def test_local_only_silent_collector_sends_nothing():
    assert _deliver_result(
        {"id": "silent-collector", "deliver": "local"},
        "routine success",
        adapters={},
        loop=None,
    ) is None
