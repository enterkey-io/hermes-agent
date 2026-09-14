import json
import threading
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from agent.turn_finalizer import finalize_turn


class FakeAgent:
    def __init__(self):
        self.max_iterations = 90
        self.iteration_budget = SimpleNamespace(remaining=10, used=1, max_total=90)
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = True
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self.persisted_messages: list[dict[str, Any]] | None = None
        self._persist_user_message_idx: int | None = None
        self._persist_user_message_override: Any = None
        self._persist_user_message_timestamp: float | None = None

    def _handle_max_iterations(self, messages, api_call_count):
        raise AssertionError("not expected")

    def _emit_status(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _save_trajectory(self, *_args, **_kwargs):
        pass

    def _cleanup_task_resources(self, *_args, **_kwargs):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        # Capture the durable write before finalization restores API-local
        # guidance to the returned/live transcript.
        self.persisted_messages = [dict(message) for message in messages]

    def _apply_persist_user_message_override(self, messages):
        idx = self._persist_user_message_idx
        override = self._persist_user_message_override
        if idx is not None and override is not None:
            messages[idx]["content"] = override

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_kwargs):
        pass






def test_final_response_closes_tool_tail_before_persistence(monkeypatch):
    """A recovered/previewed final response must be durable in session history.

    Regression for turns where the caller receives a non-empty final_response,
    but the message transcript still ends at a tool result. If persisted that
    way, the next turn reloads a stale/malformed history and can appear to loop
    because the assistant's visible final answer is missing from durable state.
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "I'll check.",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "terminal", "content": "ok"},
    ]

    result = finalize_turn(
        agent,
        final_response="Done.",
        api_call_count=2,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="do it",
        original_user_message="do it",
        _should_review_memory=False,
        _turn_exit_reason="fallback_prior_turn_content",
    )

    assert result["messages"][-1]["role"] == "assistant"
    assert result["messages"][-1]["content"] == "Done."
    assert isinstance(result["messages"][-1]["timestamp"], float)
    assert agent.persisted_messages is not None
    assert agent.persisted_messages[-1] == result["messages"][-1]


def test_fallback_timestamp_survives_delayed_sqlite_persistence(
    monkeypatch, tmp_path
):
    """The durable row records message creation, not the later DB flush."""
    from hermes_state import SessionDB

    created_at = 1_781_976_577.25
    persisted_at = created_at + 600
    monkeypatch.setattr("agent.message_metadata.wall_time", lambda: created_at)
    monkeypatch.setattr("hermes_state.time.time", lambda: persisted_at)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("sess-test", source="cli")
    agent = FakeAgent()

    def persist_to_sqlite(messages, _conversation_history):
        db.replace_messages(agent.session_id, messages)
        agent.persisted_messages = db.get_messages_as_conversation(agent.session_id)

    agent._persist_session = persist_to_sqlite
    messages = [
        {"role": "user", "content": "do it", "timestamp": created_at - 1},
        {"role": "tool", "content": "ok", "tool_call_id": "call-1"},
    ]

    finalize_turn(
        agent,
        final_response="Done.",
        api_call_count=2,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="do it",
        original_user_message="do it",
        _should_review_memory=False,
        _turn_exit_reason="fallback_prior_turn_content",
    )

    assert agent.persisted_messages[-1]["timestamp"] == created_at
    assert agent.persisted_messages[-1]["timestamp"] != persisted_at


def test_final_response_fills_pure_tool_call_tail(monkeypatch):
    """A tail assistant row that is a *pure tool-call turn* carries no answer.

    The role check alone ("tail is assistant ⇒ nothing to do") leaves the
    #43849/#44100 invariant unmet when the tail is ``assistant(tool_calls)``
    with no text of its own: the caller and the gateway already delivered
    ``final_response``, but it never reaches the transcript. The next turn then
    replays the user backlog and the model re-answers it — the exact symptom
    that block exists to prevent.
    """
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}}
            ],
        },
    ]

    result = finalize_turn(
        agent,
        final_response="Here is your answer.",
        api_call_count=3,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="t",
        turn_id="tid",
        user_message="q",
        original_user_message="q",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )

    persisted = agent.persisted_messages
    assert any(
        m.get("role") == "assistant" and m.get("content") == result["final_response"]
        for m in persisted
    ), "delivered final_response never reached the durable transcript"
    # Filled in place — no assistant→assistant pair, tool_calls preserved.
    assert persisted[-1]["content"] == "Here is your answer."
    assert persisted[-1]["tool_calls"]
    assert sum(1 for m in persisted if m.get("role") == "assistant") == 1






def test_final_response_fill_invalidates_flush_scan_cursor():
    """The fill's marker pop must invalidate the bounded flush-scan cursor.

    The cursor (run_agent.py) skips the identity-matched prefix of its
    previous snapshot assuming no live dict loses ``_db_persisted`` in place
    — the fill is the one path that pops it. Without invalidation, the
    turn-end flush skips the filled row as 'already stamped' and the
    delivered answer never reaches state.db (the #43849 class resurfacing).
    """
    agent = FakeAgent()
    agent._db_flush_scan_prefix = ["prior-snapshot"]
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}}
            ],
            "_db_persisted": True,
        },
    ]

    finalize_turn(
        agent,
        final_response="Here is your answer.",
        api_call_count=3,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="t",
        turn_id="tid",
        user_message="q",
        original_user_message="q",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )

    assert agent._db_flush_scan_prefix is None


@pytest.mark.parametrize(
    "blocked_phase",
    ["trajectory", "cleanup", "persist", "micro_compact"],
)
def test_visible_final_is_signaled_before_blocking_post_turn_maintenance(
    monkeypatch,
    blocked_phase,
):
    entered = threading.Event()
    release = threading.Event()
    visible = threading.Event()
    events = []
    agent = FakeAgent()

    def maintenance(phase):
        events.append(phase)
        if phase == blocked_phase:
            entered.set()
            assert release.wait(timeout=2)

    agent._save_trajectory = lambda *_args: maintenance("trajectory")
    agent._cleanup_task_resources = lambda *_args: maintenance("cleanup")
    agent._persist_session = lambda *_args, **_kwargs: maintenance("persist")

    def signal_visible(text):
        events.append(("visible", text))
        visible.set()

    agent.stream_final_callback = signal_visible

    def micro_compact(messages):
        maintenance("micro_compact")
        return messages

    agent.context_compressor = SimpleNamespace(
        last_prompt_tokens=0,
        _micro_compact_enabled=True,
        _micro_compact=micro_compact,
    )

    def invoke_hook(name, **_kwargs):
        if name == "transform_llm_output":
            return ["Raw answer\n\n[transformed]"]
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", invoke_hook)
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "Raw answer"},
    ]

    result_holder = []
    errors = []

    def finalize():
        try:
            result_holder.append(
                finalize_turn(
                    agent,
                    final_response="Raw answer",
                    api_call_count=1,
                    interrupted=False,
                    failed=False,
                    messages=messages,
                    conversation_history=[],
                    effective_task_id="task",
                    turn_id="turn",
                    user_message="question",
                    original_user_message="question",
                    _should_review_memory=False,
                    _turn_exit_reason="text_response(final)",
                )
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=finalize)
    thread.start()
    try:
        assert entered.wait(timeout=1)
        assert visible.is_set(), f"delivery was not signaled before {blocked_phase}"
    finally:
        release.set()
        thread.join(timeout=2)

    assert thread.is_alive() is False
    assert errors == []
    assert result_holder[0]["final_response"] == "Raw answer\n\n[transformed]"
    assert events == [
        ("visible", "Raw answer\n\n[transformed]"),
        "trajectory",
        "cleanup",
        "persist",
        "micro_compact",
    ]


def test_post_micro_compaction_rewrites_enabled_json_snapshot(monkeypatch, tmp_path):
    from run_agent import AIAgent

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._session_json_enabled = True
    agent.logs_dir = tmp_path
    agent.session_start = datetime.now()
    agent.platform = "telegram"
    agent._cached_system_prompt = ""
    agent.tools = []
    agent.verbose_logging = False
    agent._clean_session_content = lambda content: content
    agent._redact_message_content = lambda content: content
    agent._save_session_log = AIAgent._save_session_log.__get__(agent)

    def persist(
        messages,
        _conversation_history,
        *,
        allow_json_snapshot_shrink=False,
        preserve_json_snapshot=False,
    ):
        if preserve_json_snapshot:
            return
        if allow_json_snapshot_shrink:
            agent._save_session_log(messages, allow_shrink=True)
        else:
            agent._save_session_log(messages)

    agent._persist_session = persist

    def micro_compact(messages):
        return messages[-2:]

    agent.context_compressor = SimpleNamespace(
        last_prompt_tokens=0,
        _micro_compact_enabled=True,
        _micro_compact=micro_compact,
        _last_micro_compact_db_sync_succeeded=True,
    )
    messages = [
        {"role": "user", "content": "older question"},
        {"role": "assistant", "content": "older answer"},
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": "current answer"},
    ]

    finalize_turn(
        agent,
        final_response="current answer",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="current question",
        original_user_message="current question",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )

    snapshot = json.loads(
        (tmp_path / "session_sess-test.json").read_text(encoding="utf-8")
    )
    assert snapshot["message_count"] == 2
    assert [message["content"] for message in snapshot["messages"]] == [
        "current question",
        "current answer",
    ]


def test_failed_db_micro_compaction_cannot_shrink_json_snapshot(
    monkeypatch,
    tmp_path,
):
    from agent.context_compressor import ContextCompressor
    from hermes_state import SessionDB
    from run_agent import AIAgent

    class ArchiveFailingSessionDB(SessionDB):
        def archive_and_compact(self, *_args, **_kwargs):
            raise RuntimeError("injected archive failure")

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_a, **_kw: [])
    db_path = tmp_path / "state.db"
    db = ArchiveFailingSessionDB(db_path=db_path)
    db.create_session("sess-test", source="telegram")

    agent = FakeAgent()
    agent._session_json_enabled = True
    agent.logs_dir = tmp_path / "sessions"
    agent.logs_dir.mkdir()
    agent.session_start = datetime.now()
    agent.platform = "telegram"
    agent._cached_system_prompt = ""
    agent.tools = []
    agent.verbose_logging = False
    agent._clean_session_content = lambda content: content
    agent._redact_message_content = lambda content: content
    agent._session_db = db
    agent._session_db_created = True
    agent._persist_disabled = False
    agent._session_persist_lock = None
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._last_flushed_db_idx = 0
    agent._db_flush_scan_prefix = None
    agent._pending_cli_user_message = None
    agent._active_compression_lock_holder = None
    agent._active_session_turn_lease_holder = None
    agent._active_session_turn_lease_ttl_seconds = 300.0
    agent._last_persistence_error_cause = None
    agent._compression_adoption_failed = False
    agent._inflight_turn_id = None
    agent._inflight_turn_session_id = None
    agent._ensure_db_session = lambda: None
    real_save_session_log = AIAgent._save_session_log.__get__(agent)
    json_save_calls = []

    def save_session_log(*args, **kwargs):
        json_save_calls.append(kwargs.get("allow_shrink", False))
        return real_save_session_log(*args, **kwargs)

    agent._save_session_log = save_session_log
    agent._flush_messages_to_session_db = (
        AIAgent._flush_messages_to_session_db.__get__(agent)
    )
    agent._flush_messages_to_session_db_unlocked = (
        AIAgent._flush_messages_to_session_db_unlocked.__get__(agent)
    )
    agent._persist_session = AIAgent._persist_session.__get__(agent)

    compressor = ContextCompressor(
        model="test-model",
        threshold_percent=0.75,
        protect_first_n=0,
        protect_last_n=2,
        quiet_mode=True,
        config_context_length=40960,
        provider="test",
    )
    compressor._micro_compact_enabled = True
    compressor._micro_summarize_one = lambda _text: "ROLLING SUMMARY"
    compressor._session_db = db
    compressor._session_id = agent.session_id
    agent.context_compressor = compressor
    messages = [
        {"role": "user", "content": "question 0"},
        {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "content": "result",
            "tool_call_id": "call-1",
        },
        {"role": "user", "content": "question 1"},
        {"role": "assistant", "content": "answer 1"},
        {"role": "user", "content": "question 2"},
        {"role": "assistant", "content": "answer 2"},
    ]
    original_contents = [message["content"] for message in messages]

    finalize_turn(
        agent,
        final_response="answer 2",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="question 2",
        original_user_message="question 2",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )

    snapshot = json.loads(
        (agent.logs_dir / "session_sess-test.json").read_text(encoding="utf-8")
    )
    assert compressor._last_micro_compact_db_sync_succeeded is False
    assert json_save_calls == [False]
    assert snapshot["message_count"] == len(original_contents)
    assert [message["content"] for message in snapshot["messages"]] == original_contents

    durable = db.get_messages_as_conversation(agent.session_id)
    assert len(durable) > len(original_contents)
    assert [message["content"] for message in durable[: len(original_contents)]] == (
        original_contents
    )


def test_current_assistant_row_identity_never_falls_back_to_prior_turn(monkeypatch):
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "earlier answer", "_row_id": 77},
        {"role": "user", "content": "current"},
        {"role": "assistant", "content": "current answer"},
    ]

    result = finalize_turn(
        agent,
        final_response="current answer",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=messages[:2],
        effective_task_id="task",
        turn_id="turn",
        user_message="current",
        original_user_message="current",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )

    assert result["assistant_message_row_id"] is None


def test_post_micro_compaction_persist_failure_is_reported(monkeypatch):
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    persist_calls = 0

    def persist(*_args, **kwargs):
        nonlocal persist_calls
        persist_calls += 1
        if persist_calls == 2:
            assert kwargs == {
                "allow_json_snapshot_shrink": True,
                "preserve_json_snapshot": False,
            }
            raise RuntimeError("mirror write failed")

    def micro_compact(messages):
        messages[-1]["_row_id"] = 99
        return messages

    agent._persist_session = persist
    agent.context_compressor = SimpleNamespace(
        last_prompt_tokens=0,
        _micro_compact_enabled=True,
        _micro_compact=micro_compact,
        _last_micro_compact_db_sync_succeeded=True,
    )

    result = finalize_turn(
        agent,
        final_response="answer",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=[
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="question",
        original_user_message="question",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )

    assert result["cleanup_errors"] == [
        "persist_session_after_micro_compaction: mirror write failed"
    ]
