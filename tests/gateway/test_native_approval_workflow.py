"""Native gateway notification and button resolution around real terminal work."""

import asyncio
import json
import shlex
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from tests.gateway.test_telegram_approval_buttons import TelegramAdapter
from tools import approval


@pytest.mark.asyncio
@pytest.mark.parametrize("expire_before_click", [False, True])
async def test_gateway_native_prompt_to_real_terminal(monkeypatch, tmp_path, expire_before_click):
    from gateway.run import TurnRunner
    from tools.terminal_tool import cleanup_all_environments, terminal_tool

    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "*")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 60)
    monkeypatch.setattr(approval, "_gateway_queues", {})
    monkeypatch.setattr(approval, "_gateway_expired", {})
    monkeypatch.setattr(approval, "_session_approved", {})
    monkeypatch.setattr(approval, "_permanent_approved", set())
    observed = []
    marker = tmp_path / "executed.txt"
    code = f"from pathlib import Path; Path({str(marker)!r}).write_text('executed')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    class ClickingTelegram(TelegramAdapter):
        async def send_exec_approval(self, **kwargs):
            result = await super().send_exec_approval(**kwargs)
            assert result.success
            entry = approval._gateway_queues[kwargs["session_key"]][0]
            observed.append((kwargs["request_id"], entry.data["request_id"]))
            if expire_before_click:
                entry.expires_at = 0
            query = AsyncMock()
            query.data = f"ea:once:{next(iter(self._approval_state))}"
            query.message = MagicMock(chat_id=12345)
            query.from_user = SimpleNamespace(id=12345, first_name="Owner")
            await self._handle_callback_query(SimpleNamespace(callback_query=query), MagicMock())
            return result

    adapter = ClickingTelegram(PlatformConfig(enabled=True, token="fixture"))
    adapter._bot = AsyncMock()
    adapter._bot.send_message.return_value = SimpleNamespace(message_id=42)
    adapter._app = MagicMock()

    class TerminalAgent:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]
            self.session_id = kwargs["session_id"]
            self.tools = []
            self.context_compressor = SimpleNamespace(last_prompt_tokens=0, context_length=200_000)
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0

        def run_conversation(self, _message, **_kwargs):
            result = json.loads(terminal_tool(command, workdir=str(tmp_path)))
            return {"final_response": json.dumps(result), "messages": []}

    runner = MagicMock()
    runner.config = SimpleNamespace(streaming=None)
    runner._provider_routing = {}
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._session_db = None
    runner._prefill_messages = None
    runner._pending_model_notes = {}
    runner._pending_skills_reload_notes = {}
    runner.session_store._entries = {}
    runner._get_system_prompt_for_channel.return_value = None
    runner._resolve_session_agent_runtime.return_value = ("fixture-model", {})
    runner._resolve_session_reasoning_config.return_value = None
    runner._resolve_session_service_tier.return_value = None
    runner._resolve_turn_agent_config.return_value = {"model": "fixture-model", "runtime": {}}
    runner._agent_config_signature.return_value = ("fixture-signature",)
    runner._extract_cache_busting_config.return_value = {}
    runner._refresh_fallback_model.return_value = None
    runner._consume_pending_native_image_paths.return_value = []
    runner._consume_pending_turn_sidecar_notes.return_value = []
    runner._is_telegram_topic_lane.return_value = False
    runner._is_discord_auto_thread_lane.return_value = False
    runner._is_relay_discord_channel_lane.return_value = False
    runner._adapter_for_source.return_value = adapter
    ctx = TurnContext(
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="12345", user_id="12345"),
        message="run fixture", history=[], session_id="fixture-session",
        session_key="agent:main:telegram:12345", user_config={}, AIAgent=TerminalAgent,
        resolve_display_setting=lambda *_args: False, _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False), _status_adapter=adapter,
        _status_chat_id="12345", _loop_for_step=asyncio.get_running_loop(),
    )
    try:
        result = await asyncio.to_thread(TurnRunner(runner, ctx).run_sync)
        terminal_result = json.loads(result["final_response"])
        assert len(observed) == 1
        assert observed[0][0] and observed[0][0] == observed[0][1]
        if expire_before_click:
            assert terminal_result["exit_code"] == -1
            assert not marker.exists()
        else:
            assert terminal_result["exit_code"] == 0, terminal_result
            assert marker.read_text() == "executed"
        assert not approval._session_approved
        assert not approval._permanent_approved
    finally:
        cleanup_all_environments()
