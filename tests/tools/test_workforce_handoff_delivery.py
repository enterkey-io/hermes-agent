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


def test_named_board_worker_persists_handoff_on_canonical_board(
    board, monkeypatch,
):
    named_board = board.parent / "kanban" / "boards" / "side-project" / "kanban.db"
    kanban_db.init_db(named_board)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "side-project")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(named_board))
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
    task_id = response["result"]["task_id"]
    assert kanban_db.canonical_coordination_db_path() == board
    with kanban_db.connect_closing(board) as conn:
        assert kanban_db.get_task(conn, task_id) is not None
    with kanban_db.connect_closing(named_board) as conn:
        assert kanban_db.get_task(conn, task_id) is None


def test_malformed_board_pin_does_not_disable_canonical_handoffs(
    board, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "../not-a-board")

    assert kanban_db.canonical_coordination_db_path() == board
    monkeypatch.delenv("HERMES_KANBAN_DB")
    assert kanban_db.canonical_coordination_db_path() == board


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


def test_coordinated_handoff_uses_its_request_bound_named_board(
    board, monkeypatch,
):
    named_board = board.parent / "kanban" / "boards" / "side-project" / "kanban.db"
    kanban_db.init_db(named_board)
    org = load_organization(ORG_PATH)
    with kanban_db.connect_closing(named_board) as conn:
        root_id = kanban_db.create_task(
            conn,
            title="Return the named-board request",
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

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "side-project")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(named_board))
    tokens = set_session_vars(
        platform="telegram",
        chat_id="origin-chat",
        chat_type="dm",
        session_id="origin-session",
        message_id="origin-message",
        profile="alina",
    )
    try:
        with scoped_coordination_budget(
            request_root_id=request.id,
            task_id=root_id,
            purpose="work",
            db_path=named_board,
        ):
            response = json.loads(_handle(_create_args()))
    finally:
        clear_session_vars(tokens)

    assert response["success"] is True
    task_id = response["result"]["task_id"]
    with kanban_db.connect_closing(named_board) as conn:
        from hermes_cli.workforce_handoffs import acknowledge_handoff

        task = kanban_db.get_task(conn, task_id)
        assert task is not None
        assert task.request_root_id == request.id
        acknowledge_handoff(conn, task_id, actor="aurora", organization=org)

    with scoped_coordination_budget(
        request_root_id=request.id,
        task_id=task_id,
        purpose="work",
        db_path=named_board,
    ):
        checkpoint = json.loads(_handle({
            "action": "checkpoint",
            "task_id": task_id,
            "evidence_references": ["execution:named-board-checkpoint"],
        }))

    assert checkpoint["success"] is True
    assert checkpoint["result"]["state"] == "active"
    worker_bound_checkpoint = json.loads(_handle({
        "action": "checkpoint",
        "task_id": task_id,
        "evidence_references": ["execution:worker-bound-checkpoint"],
    }))
    assert worker_bound_checkpoint["success"] is True
    with kanban_db.connect_closing(named_board) as conn:
        task = kanban_db.get_task(conn, task_id)
        assert task is not None
        assert json.loads(task.body)["checkpoint_evidence"] == [
            "execution:worker-bound-checkpoint"
        ]
    with kanban_db.connect_closing(board) as conn:
        assert kanban_db.get_task(conn, task_id) is None


@pytest.mark.parametrize("fail_first", [False, True])
def test_ordinary_handoff_completion_wakes_original_source_session(
    board, monkeypatch, fail_first,
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

    # A worker process may still carry its named-board pins while the gateway
    # notifier performs machine-wide delivery. Those pins must not redirect
    # collection or the later cursor mutation away from the canonical handoff.
    monkeypatch.delenv("HERMES_KANBAN_DB")
    kanban_db.create_board("side-project")
    named_board = kanban_db.kanban_db_path("side-project").resolve()
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "side-project")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(named_board))

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
    if fail_first:
        wake.side_effect = RuntimeError("temporary wake failure")
    monkeypatch.setattr("gateway.wake.deliver_wake", wake)
    real_sleep = asyncio.sleep

    async def stop_after_tick(delay):
        if delay != 5:
            runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", stop_after_tick)
    asyncio.run(runner._kanban_notifier_loop(interval=0.01))

    if fail_first:
        wake.assert_awaited_once()
        with kanban_db.connect_closing(board) as conn:
            assert kanban_db.list_notify_subs(conn, task_id)[0][
                "last_event_id"
            ] == original_cursor
        wake.reset_mock(side_effect=True)
        runner._running = True
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
    with kanban_db.connect_closing(named_board) as conn:
        assert kanban_db.list_notify_subs(conn) == []


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


def test_global_sweep_flags_named_board_despite_worker_redirect(board, monkeypatch):
    from hermes_cli.workforce_handoffs import create_handoff

    monkeypatch.delenv("HERMES_KANBAN_DB")
    kanban_db.create_board("side-project")
    named_board = kanban_db.kanban_db_path("side-project").resolve()
    broken_board = kanban_db.board_dir("aa-broken") / "kanban.db"
    broken_board.parent.mkdir(parents=True)
    broken_board.write_bytes(b"not a sqlite database")
    now = datetime.now(timezone.utc)
    with kanban_db.connect_closing(named_board) as conn:
        created = create_handoff(
            conn,
            source_agent="alina",
            target_agent="aurora",
            expected_outcome="Complete the overdue named-board work",
            acceptance_test="The global sweep flags this exact handoff",
            evidence_references=["execution:named-sweep"],
            acknowledgment_deadline=(now - timedelta(minutes=2)).isoformat(),
            checkpoint_at=(now + timedelta(minutes=20)).isoformat(),
            allow_overdue=True,
        )

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "side-project")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(named_board))
    monkeypatch.setattr("tools.workforce_handoff_tool._source", lambda: "aurora")
    response = json.loads(_handle({"action": "sweep"}))

    assert response["success"] is True
    assert created["task_id"] in {
        item["task_id"] for item in response["result"]["changed"]
    }
    with kanban_db.connect_closing(named_board) as conn:
        task = kanban_db.get_task(conn, created["task_id"])
        assert task is not None
        assert task.status == "blocked"
        assert json.loads(task.body)["state"] == "acknowledgment_overdue"
    with kanban_db.connect_closing(board) as conn:
        assert kanban_db.get_task(conn, created["task_id"]) is None
    assert broken_board.read_bytes() == b"not a sqlite database"


def test_global_sweep_preserves_actor_authorization(board):
    response = json.loads(_handle({"action": "sweep"}))
    assert response.get("success") is not True
    assert "only Aurora or Chloe" in response["error"]
