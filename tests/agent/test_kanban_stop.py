"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB"):
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






# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.
