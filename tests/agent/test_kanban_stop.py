"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import logging

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    kanban_stop_requires_failure_recovery,
    latest_operational_failure,
    session_called_kanban_terminal,
)
from agent.turn_finalizer import _record_kanban_operational_failure


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_STOP_NUDGE",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_DB",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture
def worker_board(tmp_path, clear_kanban_env):
    from hermes_cli import kanban_db as kb

    path = tmp_path / "board.db"
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(path))
    with kb.connect_closing(path) as conn:
        tid = kb.create_task(conn, title="Bounded handoff", assignee="worker")
        assert kb.claim_task(conn, tid)
        run = kb.latest_run(conn, tid)
        clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
        clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run.id))
        yield kb, conn, tid, run.id


def test_cli_scheduled_handoff_does_not_nudge(worker_board):
    from argparse import Namespace
    from hermes_cli.kanban import _cmd_schedule

    kb, conn, tid, run_id = worker_board
    assert build_kanban_stop_nudge(messages=[]) is not None
    assert _cmd_schedule(Namespace(task_id=tid, reason=["Await manager release"])) == 0
    assert kb.get_task(conn, tid).status == "scheduled"
    assert kb.get_run(conn, run_id).outcome == "scheduled"
    assert build_kanban_stop_nudge(messages=[]) is None


def test_conversation_exits_after_persisted_cli_park(worker_board):
    from argparse import Namespace
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from hermes_cli.kanban import _cmd_schedule
    from run_agent import AIAgent

    _, _, tid, _ = worker_board
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are a bounded worker."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False

    def finish(*args, **kwargs):
        assert _cmd_schedule(Namespace(task_id=tid, reason=["Await release"])) == 0
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="Handoff parked.", tool_calls=None),
                finish_reason="stop",
            )],
            model="test/model",
            usage=None,
        )

    agent.client.chat.completions.create.side_effect = finish
    result = agent.run_conversation("Record the bounded handoff.", task_id=tid)
    assert result["final_response"] == "Handoff parked."
    assert agent.client.chat.completions.create.call_count == 1
    assert getattr(agent, "_kanban_stop_nudges", 0) == 0


def test_scheduled_other_task_does_not_satisfy_guard(worker_board):
    kb, conn, tid, run_id = worker_board
    other = kb.create_task(conn, title="Other", assignee="worker")
    kb.claim_task(conn, other)
    assert kb.schedule_task(conn, other)
    assert build_kanban_stop_nudge(messages=[]) is not None


def test_previous_scheduled_run_does_not_satisfy_new_run(worker_board, monkeypatch):
    kb, conn, tid, run_id = worker_board
    assert kb.schedule_task(conn, tid, expected_run_id=run_id)
    assert kb.unblock_task(conn, tid)
    assert build_kanban_stop_nudge(messages=[]) is not None
    assert kb.claim_task(conn, tid)
    newer = kb.latest_run(conn, tid).id
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(newer))
    assert not kb.schedule_task(conn, tid, expected_run_id=run_id)
    assert build_kanban_stop_nudge(messages=[]) is not None
    assert kb.schedule_task(conn, tid, expected_run_id=newer)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    assert build_kanban_stop_nudge(messages=[]) is not None
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(newer))
    assert build_kanban_stop_nudge(messages=[]) is None


@pytest.mark.parametrize("run_id", ["", "invalid", "0", "-1", "999999"])
def test_scheduled_requires_matching_run(worker_board, monkeypatch, run_id):
    kb, conn, tid, actual_run = worker_board
    assert kb.schedule_task(conn, tid, expected_run_id=actual_run)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    assert build_kanban_stop_nudge(messages=[]) is not None


def test_terminal_stdout_and_narration_are_not_schedule_proof(worker_board):
    _, _, tid, _ = worker_board
    assert build_kanban_stop_nudge(messages=[
        {"role": "tool", "name": "terminal", "content": f"Scheduled {tid}"},
        {"role": "assistant", "content": "I have parked the task."},
    ]) is not None


@pytest.mark.parametrize("contents", [None, b"invalid database", b""])
def test_missing_or_unreadable_board_still_nudges(tmp_path, clear_kanban_env, contents):
    path = tmp_path / "missing.db"
    if contents is not None:
        path.write_bytes(contents)
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_worker")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", "1")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(path))
    assert build_kanban_stop_nudge(messages=[]) is not None
    if contents is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == contents






def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_opt_out_does_not_allow_advisory_operational_failure_exit(
    clear_kanban_env,
):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    messages = [
        {
            "role": "tool",
            "name": "kanban_handoff",
            "content": '{"error": "database is locked"}',
        }
    ]

    assert build_kanban_stop_nudge(messages=messages) is None
    assert kanban_stop_requires_failure_recovery(
        messages=messages, attempts=0
    ) is True


def test_conversation_stop_guard_exception_clears_prior_recovery_state(monkeypatch):
    from agent import conversation_loop
    from agent import kanban_stop

    monkeypatch.setattr(kanban_stop, "build_kanban_stop_nudge", lambda **_k: None)
    monkeypatch.setattr(
        kanban_stop,
        "kanban_stop_requires_failure_recovery",
        lambda **_k: True,
    )
    monkeypatch.setattr(
        kanban_stop,
        "latest_operational_failure",
        lambda _messages: "prior failure",
    )
    assert conversation_loop._evaluate_kanban_stop_guard([], 0) == (
        None,
        True,
        "prior failure",
    )

    def fail_check(**_kwargs):
        raise RuntimeError("guard unavailable")

    monkeypatch.setattr(kanban_stop, "build_kanban_stop_nudge", fail_check)
    assert conversation_loop._evaluate_kanban_stop_guard([], 1) == (
        None,
        False,
        None,
    )


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "kanban_complete",
            "tool_call_id": "1",
            "content": '{"ok": true, "task_id": "t_abc", "run_id": 7}',
        },
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_no_nudge_after_successful_kanban_request_review(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "tool",
            "name": "kanban_request_review",
            "tool_call_id": "1",
            "content": {"ok": True, "task_id": "t_abc", "status": "review"},
        }
    ]

    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_no_nudge_after_successful_kanban_request_changes(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "tool",
            "name": "kanban_request_changes",
            "tool_call_id": "1",
            "content": {
                "ok": True,
                "task_id": "t_abc",
                "status": "ready",
                "implementer": "alina",
            },
        }
    ]

    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


@pytest.mark.parametrize(
    "messages",
    [
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "1",
                        "type": "function",
                        "function": {
                            "name": "kanban_request_review",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        ],
        [
            {
                "role": "tool",
                "name": "kanban_request_review",
                "content": '{"error": "transition rejected"}',
            }
        ],
        [
            {
                "role": "tool",
                "name": "kanban_request_review",
                "content": {
                    "ok": False,
                    "task_id": "t_abc",
                    "status": "review",
                },
            }
        ],
        [
            {
                "role": "tool",
                "name": "kanban_request_changes",
                "content": {
                    "ok": True,
                    "task_id": "t_abc",
                    "status": "ready",
                },
            }
        ],
        [
            {
                "role": "tool",
                "name": "kanban_request_review",
                "content": {
                    "ok": True,
                    "task_id": "t_other",
                    "status": "review",
                },
            }
        ],
        [
            {
                "role": "tool",
                "name": "kanban_request_review",
                "content": {
                    "ok": True,
                    "task_id": "t_abc",
                    "status": "running",
                },
            }
        ],
    ],
    ids=[
        "invocation-only",
        "error-result",
        "ok-false",
        "wrong-task",
        "wrong-status",
        "request-changes-missing-implementer",
    ],
)
def test_failed_or_unobserved_terminal_call_still_nudges(
    clear_kanban_env, messages,
):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")

    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages) is not None






@pytest.mark.parametrize(
    "tool_name",
    [
        "kanban_handoff",
        "kanban_pass_review",
    ],
)
def test_no_nudge_after_successful_lifecycle_phase_transition(
    clear_kanban_env, tool_name
):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_phase")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "phase-1",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": tool_name,
            "tool_call_id": "phase-1",
            "content": (
                '{"ok": true, "task_id": "t_phase", "status": "ready"}'
            ),
        },
    ]

    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


@pytest.mark.parametrize(
    "failure_result",
    [
        '{"error": "current worker run is stale"}',
        '{"ok": false, "error": "ownership check failed"}',
        "Error: lifecycle transition failed",
    ],
)
def test_rejected_lifecycle_transition_does_not_authorize_advisory_exit(
    clear_kanban_env, failure_result
):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_failed_transition")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "transition-1",
                    "type": "function",
                    "function": {
                        "name": "kanban_handoff",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": "kanban_handoff",
            "tool_call_id": "transition-1",
            "content": failure_result,
        },
    ]

    assert session_called_kanban_terminal(messages) is False
    nudge = build_kanban_stop_nudge(messages=messages, attempts=1, max_attempts=2)
    assert nudge is not None
    assert "operational failure" in nudge.lower()
    assert "durable" in nudge.lower()
    assert build_kanban_stop_nudge(
        messages=messages, attempts=2, max_attempts=2
    ) is None
    assert kanban_stop_requires_failure_recovery(
        messages=messages, attempts=2, max_attempts=2
    ) is True


def test_lifecycle_intent_without_result_does_not_count_as_durable_action(
    clear_kanban_env,
):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_unacknowledged")
    messages = [
        {
            "role": "assistant",
            "content": "I will hand this off now.",
            "tool_calls": [
                {
                    "id": "transition-1",
                    "type": "function",
                    "function": {
                        "name": "kanban_handoff",
                        "arguments": "{}",
                    },
                }
            ],
        }
    ]

    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages) is not None


def test_successful_retry_after_operational_error_authorizes_exit(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_recovered")
    messages = [
        {
            "role": "tool",
            "name": "kanban_handoff",
            "tool_call_id": "transition-1",
            "content": '{"error": "temporary database lock"}',
        },
        {
            "role": "tool",
            "name": "kanban_handoff",
            "tool_call_id": "transition-2",
            "content": '{"ok": true, "task_id": "t_recovered"}',
        },
    ]

    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages, attempts=2) is None


def test_plain_noncompliance_remains_bounded_without_operational_error(
    clear_kanban_env,
):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_plain_stop")

    assert build_kanban_stop_nudge(
        messages=[{"role": "assistant", "content": "I will finish later."}],
        attempts=2,
        max_attempts=2,
    ) is None
    assert kanban_stop_requires_failure_recovery(
        messages=[{"role": "assistant", "content": "I will finish later."}],
        attempts=2,
        max_attempts=2,
    ) is False


def test_non_kanban_informational_answer_is_never_captured(clear_kanban_env):
    messages = [
        {
            "role": "tool",
            "name": "terminal",
            "content": '{"exit_code": 1, "output": "service unavailable"}',
        },
        {
            "role": "assistant",
            "content": "The service is currently unavailable.",
        },
    ]

    assert latest_operational_failure(messages) == "terminal: exit_code=1"
    assert build_kanban_stop_nudge(messages=messages) is None
    assert kanban_stop_requires_failure_recovery(
        messages=messages, attempts=2, max_attempts=2
    ) is False


def test_exhausted_operational_failure_durably_requeues_same_card(
    clear_kanban_env, tmp_path
):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    clear_kanban_env.setenv("HERMES_HOME", str(home))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="recover failed worker",
            assignee="builder",
        )
        running = kb.claim_task(conn, task_id, claimer="test:worker")
        assert running is not None
        assert running.status == "running"
        assert running.current_run_id is not None
        clear_kanban_env.setenv("HERMES_KANBAN_TASK", task_id)
        clear_kanban_env.setenv(
            "HERMES_KANBAN_RUN_ID", str(running.current_run_id)
        )
        clear_kanban_env.setenv(
            "HERMES_KANBAN_CLAIM_LOCK", str(running.claim_lock)
        )

        status = _record_kanban_operational_failure(
            task_id,
            "terminal: exit_code=1 (tests failed)",
            2,
            logging.getLogger(__name__),
        )

        assert status == "ready"
        landed = kb.get_task(conn, task_id)
        assert landed is not None
        assert landed.status == "ready"
        assert landed.current_run_id is None
        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.outcome == "crashed"
        assert "tests failed" in (run.error or "")
    finally:
        conn.close()


def test_operational_failure_recovery_cannot_mutate_an_unowned_card(
    clear_kanban_env,
):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "owned-card")

    assert _record_kanban_operational_failure(
        "different-card",
        "terminal: exit_code=1",
        2,
        logging.getLogger(__name__),
    ) is None


def test_operational_failure_recovery_cannot_close_a_newer_run(
    clear_kanban_env, tmp_path
):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    clear_kanban_env.setenv("HERMES_HOME", str(home))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="preserve replacement run",
            assignee="builder",
        )
        running = kb.claim_task(conn, task_id, claimer="new-owner:claim")
        assert running is not None
        clear_kanban_env.setenv("HERMES_KANBAN_TASK", task_id)
        clear_kanban_env.setenv(
            "HERMES_KANBAN_RUN_ID", str(int(running.current_run_id) + 1)
        )
        clear_kanban_env.setenv("HERMES_KANBAN_CLAIM_LOCK", "old-owner:claim")

        status = _record_kanban_operational_failure(
            task_id,
            "kanban_handoff: stale worker",
            2,
            logging.getLogger(__name__),
        )

        assert status is None
        landed = kb.get_task(conn, task_id)
        assert landed is not None
        assert landed.status == "running"
        assert landed.current_run_id == running.current_run_id
        assert landed.claim_lock == "new-owner:claim"
    finally:
        conn.close()


@pytest.mark.parametrize(
    "missing_var",
    ["HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK"],
)
def test_operational_failure_recovery_requires_complete_run_provenance(
    clear_kanban_env, tmp_path, missing_var
):
    """An unproven fallback must never close whichever run is active now."""
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    clear_kanban_env.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="preserve active run without provenance",
            assignee="builder",
        )
        running = kb.claim_task(conn, task_id, claimer="replacement:claim")
        assert running is not None
        clear_kanban_env.setenv("HERMES_KANBAN_TASK", task_id)
        clear_kanban_env.setenv(
            "HERMES_KANBAN_RUN_ID", str(running.current_run_id)
        )
        clear_kanban_env.setenv(
            "HERMES_KANBAN_CLAIM_LOCK", str(running.claim_lock)
        )
        clear_kanban_env.delenv(missing_var)

        status = _record_kanban_operational_failure(
            task_id,
            "kanban_handoff: provenance was lost",
            2,
            logging.getLogger(__name__),
        )

        assert status is None
        landed = kb.get_task(conn, task_id)
        assert landed is not None
        assert landed.status == "running"
        assert landed.current_run_id == running.current_run_id
        assert landed.claim_lock == "replacement:claim"


def test_exhausted_operational_failure_honors_card_retry_threshold(
    clear_kanban_env, tmp_path
):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    clear_kanban_env.setenv("HERMES_HOME", str(home))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="block failed worker",
            assignee="builder",
            max_retries=1,
        )
        assert kb.claim_task(conn, task_id, claimer="test:worker") is not None
        running = kb.get_task(conn, task_id)
        assert running is not None
        clear_kanban_env.setenv("HERMES_KANBAN_TASK", task_id)
        clear_kanban_env.setenv(
            "HERMES_KANBAN_RUN_ID", str(running.current_run_id)
        )
        clear_kanban_env.setenv(
            "HERMES_KANBAN_CLAIM_LOCK", str(running.claim_lock)
        )

        status = _record_kanban_operational_failure(
            task_id,
            "kanban_handoff: ownership check failed",
            2,
            logging.getLogger(__name__),
        )

        assert status == "blocked"
        landed = kb.get_task(conn, task_id)
        assert landed is not None
        assert landed.status == "blocked"
        assert landed.current_run_id is None
        assert landed.last_failure_error
        assert "ownership check failed" in landed.last_failure_error
    finally:
        conn.close()


# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.
