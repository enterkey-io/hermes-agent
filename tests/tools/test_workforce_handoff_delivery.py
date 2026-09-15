from __future__ import annotations

from datetime import datetime, timedelta, timezone
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.coordination_budget import scoped_coordination_budget
from gateway.session_context import clear_session_vars, set_session_vars
from gateway.config import Platform
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from hermes_cli import kanban_db
from hermes_cli.workforce_org import load_organization
from tools.workforce_handoff_tool import _handle


ORG_PATH = Path(__file__).parents[2] / "workforce" / "organization.yaml"


@pytest.fixture
def board(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(root / "kanban.db"))
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(ORG_PATH))
    monkeypatch.setattr("tools.workforce_handoff_tool._source", lambda: "alina")
    monkeypatch.setattr(
        "tools.kanban_tools._task_runtime_profile", lambda: "alina"
    )
    kanban_db.init_db()
    return root / "kanban.db"


def _create_args() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "action": "create",
        "target_agent": "aurora",
        "expected_outcome": "Make the routed host decision",
        "acceptance_test": "The decision is recorded with evidence",
        "evidence_references": ["kanban:t_source"],
        "acknowledgment_deadline": (now + timedelta(minutes=2)).isoformat(),
        "checkpoint_at": (now + timedelta(minutes=20)).isoformat(),
    }


def test_gateway_handoff_binds_wake_only_return_to_real_origin(board):
    tokens = set_session_vars(
        platform="telegram",
        chat_id="origin-chat",
        chat_type="dm",
        session_id="origin-session",
        message_id="origin-message",
        profile="alina",
    )
    try:
        with scoped_coordination_budget(session_id="origin-session"):
            response = json.loads(_handle(_create_args()))
    finally:
        clear_session_vars(tokens)

    assert response["success"] is True
    result = response["result"]
    assert result["request_root_id"] is None
    assert result["wake_attached"] is True
    assert result["delivery_mode"] == "session_wake"
    with kanban_db.connect_closing(board) as conn:
        task = kanban_db.get_task(conn, result["task_id"])
        assert task is not None
        assert task.session_id == "origin-session"
        assert task.request_root_id is None
        subscriptions = kanban_db.list_notify_subs(conn, task.id)
        assert len(subscriptions) == 1
        assert subscriptions[0]["platform"] == "telegram"
        assert subscriptions[0]["chat_id"] == "origin-chat"
        assert subscriptions[0]["notifier_profile"] == "alina"
        assert subscriptions[0]["delivery_mode"] == "wake"
        assert subscriptions[0]["delivery_metadata"]["origin_message_id"] == (
            "origin-message"
        )


def test_gateway_create_rolls_back_when_its_return_route_cannot_persist(
    board, monkeypatch,
):
    monkeypatch.setattr("tools.kanban_tools._maybe_auto_subscribe", lambda *a, **k: False)
    tokens = set_session_vars(
        platform="telegram",
        chat_id="origin-chat",
        chat_type="dm",
        session_id="origin-session",
        message_id="origin-message",
        profile="alina",
    )
    try:
        with scoped_coordination_budget(session_id="origin-session"):
            response = json.loads(_handle(_create_args()))
    finally:
        clear_session_vars(tokens)

    assert "success" not in response
    assert "could not attach its source return route" in response["error"]
    with kanban_db.connect_closing(board) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_notify_subs"
        ).fetchone()[0] == 0


def test_gateway_create_requires_durable_session_for_wake_return(board):
    tokens = set_session_vars(
        platform="telegram",
        chat_id="origin-chat",
        chat_type="dm",
        session_id="",
        message_id="origin-message",
        profile="alina",
    )
    try:
        with scoped_coordination_budget(session_id=""):
            response = json.loads(_handle(_create_args()))
    finally:
        clear_session_vars(tokens)

    assert "success" not in response
    assert "could not attach its source return route" in response["error"]
    with kanban_db.connect_closing(board) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_notify_subs"
        ).fetchone()[0] == 0


def test_tool_retry_cannot_rebind_handoff_to_another_origin_message(board):
    args = _create_args()
    tokens = set_session_vars(
        platform="telegram",
        chat_id="origin-chat",
        chat_type="dm",
        session_id="origin-session",
        message_id="origin-message",
        profile="alina",
    )
    try:
        with scoped_coordination_budget(session_id="origin-session"):
            first = json.loads(_handle(args))
    finally:
        clear_session_vars(tokens)

    tokens = set_session_vars(
        platform="telegram",
        chat_id="origin-chat",
        chat_type="dm",
        session_id="origin-session",
        message_id="different-message",
        profile="alina",
    )
    try:
        with scoped_coordination_budget(session_id="origin-session"):
            rebound = json.loads(_handle(args))
    finally:
        clear_session_vars(tokens)

    assert first["success"] is True
    assert "success" not in rebound
    assert "different workforce handoff or origin" in rebound["error"]
    with kanban_db.connect_closing(board) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        routes = kanban_db.list_notify_subs(conn, first["result"]["task_id"])
        assert len(routes) == 1
        assert routes[0]["delivery_metadata"]["origin_message_id"] == "origin-message"


def test_coordinated_handoff_inherits_request_and_keeps_one_final_route(board):
    org = load_organization(ORG_PATH)
    with kanban_db.connect_closing(board) as conn:
        root_id = kanban_db.create_task(
            conn,
            title="Return the accepted request",
            assignee="alina",
            session_id="origin-session",
        )
        kanban_db.add_notify_sub(
            conn,
            task_id=root_id,
            platform="telegram",
            chat_id="origin-chat",
            notifier_profile="alina",
            delivery_mode="wake",
        )
        request = kanban_db.create_coordination_request(
            conn,
            root_task_id=root_id,
            origin_session_id="origin-session",
            origin_message_id="origin-message",
            organization=org,
        )

    with scoped_coordination_budget(
        request_root_id=request.id,
        task_id=root_id,
        purpose="work",
        db_path=board,
    ):
        response = json.loads(_handle(_create_args()))

    assert response["success"] is True
    result = response["result"]
    assert result["request_root_id"] == request.id
    assert result["delivery_mode"] == "request_final_return"
    assert result["wake_attached"] is True
    with kanban_db.connect_closing(board) as conn:
        task = kanban_db.get_task(conn, result["task_id"])
        assert task is not None
        assert task.request_root_id == request.id
        assert task.session_id == "origin-session"
        assert kanban_db.list_notify_subs(conn, task.id) == []
        assert len(kanban_db.list_notify_subs(conn, root_id)) == 1


def test_ordinary_handoff_completion_wakes_original_source_session(
    board, monkeypatch,
):
    tokens = set_session_vars(
        platform="telegram",
        chat_id="origin-chat",
        chat_type="dm",
        session_id="origin-session",
        message_id="origin-message",
        profile="alina",
    )
    try:
        with scoped_coordination_budget(session_id="origin-session"):
            response = json.loads(_handle(_create_args()))
    finally:
        clear_session_vars(tokens)
    task_id = response["result"]["task_id"]

    org = load_organization(ORG_PATH)
    with kanban_db.connect_closing(board) as conn:
        from hermes_cli.workforce_handoffs import acknowledge_handoff

        acknowledge_handoff(conn, task_id, actor="aurora", organization=org)
        claimed = kanban_db.claim_task(conn, task_id, claimer="aurora:test")
        assert claimed is not None
        assert kanban_db.complete_task(
            conn,
            task_id,
            summary="Verified routed work is complete",
            expected_run_id=claimed.current_run_id,
        )
        original_cursor = kanban_db.list_notify_subs(conn, task_id)[0][
            "last_event_id"
        ]

    adapter = SimpleNamespace(send=AsyncMock())

    class Runner(GatewayKanbanWatchersMixin):
        def __init__(self):
            self._running = True
            self.adapters = {Platform.TELEGRAM: adapter}
            self._profile_adapters = {}
            self._kanban_coordination_jobs = {}

        def _active_profile_name(self):
            return "alina"

        def _owns_kanban_dispatcher_lock(self):
            return False

        def _authorization_adapter(self, platform, profile):
            assert platform == Platform.TELEGRAM
            assert profile == "alina"
            return adapter

    runner = Runner()
    runner._kanban_coordination_tick = AsyncMock()
    wake = AsyncMock(return_value=SimpleNamespace(
        state="acknowledged", returned_message_id="wake:1",
    ))
    monkeypatch.setattr("gateway.wake.deliver_wake", wake)
    real_sleep = asyncio.sleep

    async def stop_after_tick(delay):
        if delay != 5:
            runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", stop_after_tick)
    asyncio.run(runner._kanban_notifier_loop(interval=0.01))

    wake.assert_awaited_once()
    assert wake.call_args.kwargs["session_id"] == "origin-session"
    assert wake.call_args.kwargs["source"].profile == "alina"
    assert "Verified routed work is complete" in wake.call_args.kwargs["text"]
    adapter.send.assert_not_awaited()
    with kanban_db.connect_closing(board) as conn:
        assert kanban_db.list_notify_subs(conn, task_id)[0][
            "last_event_id"
        ] > original_cursor


def test_overdue_handoff_wakes_source_with_blocked_outcome(board, monkeypatch):
    from hermes_cli.workforce_handoffs import sweep_overdue_handoffs

    tokens = set_session_vars(
        platform="telegram",
        chat_id="origin-chat",
        chat_type="dm",
        session_id="origin-session",
        message_id="origin-message",
        profile="alina",
    )
    try:
        with scoped_coordination_budget(session_id="origin-session"):
            response = json.loads(_handle(_create_args()))
    finally:
        clear_session_vars(tokens)
    task_id = response["result"]["task_id"]
    deadline = response["result"]["acknowledgment_deadline"]

    with kanban_db.connect_closing(board) as conn:
        original_cursor = kanban_db.list_notify_subs(conn, task_id)[0][
            "last_event_id"
        ]
        changed = sweep_overdue_handoffs(
            conn,
            actor="aurora",
            organization=load_organization(ORG_PATH),
            now=deadline + 1,
        )
        assert changed[0]["task_id"] == task_id
        assert changed[0]["state"] == "acknowledgment_overdue"

    adapter = SimpleNamespace(send=AsyncMock())

    class Runner(GatewayKanbanWatchersMixin):
        def __init__(self):
            self._running = True
            self.adapters = {Platform.TELEGRAM: adapter}
            self._profile_adapters = {}
            self._kanban_coordination_jobs = {}

        def _active_profile_name(self):
            return "alina"

        def _owns_kanban_dispatcher_lock(self):
            return False

        def _authorization_adapter(self, platform, profile):
            assert platform == Platform.TELEGRAM
            assert profile == "alina"
            return adapter

    runner = Runner()
    runner._kanban_coordination_tick = AsyncMock()
    wake = AsyncMock(return_value=SimpleNamespace(
        state="acknowledged", returned_message_id="wake:blocked",
    ))
    monkeypatch.setattr("gateway.wake.deliver_wake", wake)
    real_sleep = asyncio.sleep

    async def stop_after_tick(delay):
        if delay != 5:
            runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", stop_after_tick)
    asyncio.run(runner._kanban_notifier_loop(interval=0.01))

    wake.assert_awaited_once()
    assert wake.call_args.kwargs["session_id"] == "origin-session"
    assert "blocked" in wake.call_args.kwargs["text"].lower()
    adapter.send.assert_not_awaited()
    with kanban_db.connect_closing(board) as conn:
        task = kanban_db.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "capability"
        assert kanban_db.list_notify_subs(conn, task_id)[0][
            "last_event_id"
        ] > original_cursor
