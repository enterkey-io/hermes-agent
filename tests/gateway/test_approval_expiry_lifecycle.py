"""Expired gateway requests must remain distinguishable from absent requests."""

import asyncio
import json
import shlex
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from tools import approval


@pytest.fixture(params=["telegram", "matrix", "buzz"])
def scope(monkeypatch, tmp_path, request):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._pending_approvals = {}
    runner.adapters = {}
    runner.session_store = MagicMock()
    source = SessionSource(
        platform=Platform(request.param), user_id="owner", chat_id="approval-test",
        chat_type="group", thread_id="approval-thread",
    )
    key = runner._session_key_for_source(source)
    token = approval.set_current_session_key(key)
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 0)
    approval._gateway_queues.clear()
    approval._gateway_expired.clear()
    approval._session_approved.clear()
    approval._permanent_approved.clear()
    notifications = []
    approval.register_gateway_notify(key, notifications.append)
    yield SimpleNamespace(
        runner=runner, source=source, key=key, notifications=notifications,
        tmp_path=tmp_path,
    )
    approval.unregister_gateway_notify(key)
    approval.reset_current_session_key(token)
    from tools.terminal_tool import cleanup_all_environments

    cleanup_all_environments()


def _approve(scope):
    return asyncio.run(scope.runner._handle_approve_command(
        MessageEvent(text="/approve", source=scope.source, message_id="reply")
    ))


def _run_marker(scope):
    from tools.terminal_tool import terminal_tool

    path = scope.tmp_path / "executed.txt"
    code = f"from pathlib import Path; Path({str(path)!r}).write_text('executed')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    return path, json.loads(terminal_tool(command, workdir=str(scope.tmp_path)))


def test_real_terminal_timeout_reports_expired_after_turn_ends(scope):
    path, result = _run_marker(scope)
    assert not path.exists()
    assert result["exit_code"] == -1
    assert len(scope.notifications) == 1
    approval.unregister_gateway_notify(scope.key)
    assert "expired" in _approve(scope).lower()


def test_timeout_tool_result_does_not_invite_approval_of_closed_request(scope):
    _, result = _run_marker(scope)
    assert "no pending" in result["error"].lower()


def test_real_terminal_live_once_executes_and_is_not_expired(scope, monkeypatch):
    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 60)
    replies = []

    def notify(data):
        scope.notifications.append(data)
        replies.append(_approve(scope))

    approval.register_gateway_notify(scope.key, notify)
    path, result = _run_marker(scope)
    assert result["exit_code"] == 0, result
    assert path.read_text() == "executed"
    assert len(replies) == 1
    assert "approved" in replies[0].lower()
    assert "no pending" in _approve(scope).lower()
    assert not approval._session_approved
    assert not approval._permanent_approved


def test_expired_scope_does_not_leak_to_another_thread(scope):
    _run_marker(scope)
    source = SessionSource(
        platform=scope.source.platform, user_id="owner", chat_id="approval-test",
        chat_type="group", thread_id="unrelated-thread",
    )
    result = asyncio.run(scope.runner._handle_approve_command(
        MessageEvent(text="/approve", source=source, message_id="unrelated")
    ))
    assert "no pending" in result.lower()


def test_expired_request_cannot_be_resolved_before_worker_wakes(scope, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(approval.time, "monotonic", lambda: clock.now)
    entry = approval._ApprovalEntry({"command": "harmless", "request_id": "old"})
    entry.expires_at = 110.0
    approval._gateway_queues[scope.key] = [entry]
    clock.now = 110.0
    assert approval.resolve_gateway_approval(scope.key, "once", request_id="old") == 0
    assert entry.result is None
    assert entry.expired
    assert entry.event.is_set()
    assert approval.gateway_approval_expired(scope.key)
    assert approval.list_gateway_approvals(scope.key) == []
    assert approval.get_pending_gateway_approval(scope.key) is None
    assert not approval.ack_gateway_approval(scope.key, "old")
    assert "expired" in _approve(scope).lower()


def test_fresh_request_after_expiry_has_only_its_own_once_consent(scope, monkeypatch):
    _run_marker(scope)
    assert "expired" in _approve(scope).lower()
    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 60)
    approval.register_gateway_notify(scope.key, lambda data: _approve(scope))
    path, result = _run_marker(scope)
    assert result["exit_code"] == 0, result
    assert path.read_text() == "executed"
    assert "no pending" in _approve(scope).lower()
    assert not approval._session_approved


def test_expiry_diagnostics_are_bounded_and_store_no_commands(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(approval.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(approval, "_GATEWAY_EXPIRED_LIMIT", 2)
    monkeypatch.setattr(approval, "_gateway_expired", {})
    with approval._lock:
        for key in ("oldest", "second", "last"):
            approval._record_gateway_expiry(key)
    assert approval._gateway_expired == {"second": 100.0, "last": 100.0}
    clock.now += approval._GATEWAY_EXPIRED_TTL
    assert not approval.gateway_approval_expired("last")


@pytest.mark.parametrize("guard", ["execute_code", "plugin"])
def test_shared_guard_timeouts_explain_closed_request(scope, guard):
    if guard == "execute_code":
        result = approval.check_execute_code_guard("print('fixture')", "local")
    else:
        result = approval.request_tool_approval("fixture", "fixture requires consent")
    assert not result["approved"]
    assert "no pending" in result["message"].lower()
    assert "expired" in _approve(scope).lower()


@pytest.mark.parametrize("scope", ["buzz"], indirect=True)
@pytest.mark.parametrize("expired", [True, False])
def test_buzz_busy_adapter_routes_approval_without_queueing_or_replay(scope, monkeypatch, expired):
    from gateway.config import PlatformConfig
    from gateway.platforms.base import SendResult
    from tests.gateway._plugin_adapter_loader import load_plugin_adapter

    module = load_plugin_adapter("buzz")
    monkeypatch.setattr(module, "_DEFAULT_CREDENTIALS_DIR", scope.tmp_path / "no-creds")
    adapter = module.BuzzAdapter(PlatformConfig(enabled=True))
    deliveries = []

    async def send(chat_id, content, **kwargs):
        deliveries.append((chat_id, content, kwargs))
        return SendResult(success=True, message_id="fixture-delivery")

    async def no_reaction(*args, **kwargs):
        return True

    adapter.send = send
    adapter.send_reaction = no_reaction
    adapter.set_message_handler(scope.runner._handle_approve_command)

    async def dispatch():
        guard = asyncio.Event()
        adapter._active_sessions[scope.key] = guard
        adapter._session_tasks[scope.key] = asyncio.current_task()
        await adapter._dispatch_message(
            text="/approve", chat_id=scope.source.chat_id, chat_type="group",
            user_id=scope.source.user_id, user_name="owner", message_id="late",
            created_at=0, thread_id=scope.source.thread_id,
        )
        assert adapter._active_sessions[scope.key] is guard
        assert not adapter._pending_messages

    if expired:
        path, result = _run_marker(scope)
        asyncio.run(dispatch())
    else:
        monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 60)
        approval.register_gateway_notify(scope.key, lambda data: asyncio.run(dispatch()))
        path, result = _run_marker(scope)
    assert len(deliveries) == 1
    assert ("expired" if expired else "approved") in deliveries[0][1].lower()
    assert deliveries[0][2]["metadata"]["thread_id"] == scope.source.thread_id
    assert path.exists() is not expired
    assert result["exit_code"] == (-1 if expired else 0)
