from contextvars import copy_context
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import kanban_db
from tools import workforce_signal_tool as signal
from tools.workforce_observation_runtime import bind_buzz_events
from tools.workforce_signal_runtime import (
    activate,
    claim_turn,
    current_buzz_refs,
    mark_success,
    release_turn,
    reset,
)


SOURCE = Path(__file__).parents[2] / "workforce" / "organization.yaml"


def _payload():
    return {
        "expected_outcome": "Reduce failed releases",
        "approved_goal": "Reliable product delivery",
        "observation": "Three approved releases failed the same validation",
        "evidence_references": ["run:1", "run:2", "run:3"],
        "estimated_effort": "30 minutes to scope",
        "dependencies": ["release logs"],
        "risks": ["unknown shared cause"],
        "needed_capabilities": ["product", "agent systems"],
        "department_recommendation": "Investigate the common validator",
    }


def _bind_buzz(*, event_id="event-1", content="Service example.service failed"):
    events = [{
        "room_id": "room-1",
        "event_id": event_id,
        "author_id": "a" * 64,
        "content": content,
    }]
    bind_buzz_events(events)
    return events[0]["dedupe_ref"], events[0]["evidence_ref"]


def _invoke_workforce_turn_hook(name, **kwargs):
    from plugins.workforce_control import _on_turn_end, _on_turn_start

    if name == "on_turn_start":
        _on_turn_start(**kwargs)
    elif name == "on_turn_end":
        _on_turn_end(**kwargs)
    return []


def test_buzz_binding_distinguishes_full_messages_with_same_display_prefix():
    prefix = "x" * 600
    first_full = prefix + " first"
    second_full = prefix + " second"
    first = [{
        "room_id": "room-1", "event_id": "one", "author_id": "a" * 64,
        "content": prefix,
        "_full_content_sha256": hashlib.sha256(first_full.encode()).hexdigest(),
    }]
    second = [{
        "room_id": "room-1", "event_id": "two", "author_id": "a" * 64,
        "content": prefix,
        "_full_content_sha256": hashlib.sha256(second_full.encode()).hexdigest(),
    }]
    bind_buzz_events(first)
    bind_buzz_events(second)
    first_ref = first[0]["dedupe_ref"]
    second_ref = second[0]["dedupe_ref"]
    assert first_ref != second_ref
    assert "_full_content_sha256" not in first[0]
    assert "_full_content_sha256" not in second[0]


def test_buzz_binding_does_not_merge_different_authors():
    first = [{
        "room_id": "room-1", "event_id": "one", "author": "same-name",
        "author_id": "a" * 64,
        "content": "Service example.service failed",
    }]
    second = [{
        "room_id": "room-1", "event_id": "two", "author": "same-name",
        "author_id": "b" * 64,
        "content": "Service example.service failed",
    }]
    bind_buzz_events(first)
    bind_buzz_events(second)
    assert first[0]["dedupe_ref"] != second[0]["dedupe_ref"]


def test_optional_buzz_binding_survives_tool_worker_context_copy():
    token, state = activate(False)
    try:
        worker_context = copy_context()
        captured = {}

        def bind_in_worker():
            dedupe_ref, evidence_ref = _bind_buzz()
            captured.update(dedupe_ref=dedupe_ref, evidence_ref=evidence_ref)

        worker_context.run(bind_in_worker)
        from tools.workforce_observation_runtime import validate_buzz_signal_binding

        assert validate_buzz_signal_binding(
            dedupe_ref=captured["dedupe_ref"],
            evidence_references=[captured["evidence_ref"]],
        ) == captured["dedupe_ref"]
        assert state.buzz_refs
        assert state.track_attempts is False
    finally:
        reset(token)


def test_ordinary_conversation_real_executor_shares_buzz_binding_between_workers():
    """CLI/gateway turns bind observations across separate executor workers."""
    from run_agent import AIAgent
    from tools.workforce_observation_runtime import validate_buzz_signal_binding

    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in ("workforce_observe_buzz", "workforce_signal")
    ]
    with (
        patch("run_agent.get_tool_definitions", return_value=tool_defs),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("hermes_cli.config.load_config_readonly", return_value={}),
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
    agent.compression_enabled = False
    agent.save_trajectories = False
    observed = {}

    def dispatch(name, _args, _task_id, **_kwargs):
        if name == "workforce_observe_buzz":
            dedupe_ref, evidence_ref = _bind_buzz()
            observed.update(dedupe_ref=dedupe_ref, evidence_ref=evidence_ref)
            return json.dumps(observed)
        observed["validated"] = validate_buzz_signal_binding(
            dedupe_ref=observed["dedupe_ref"],
            evidence_references=[observed["evidence_ref"]],
        )
        return json.dumps({"success": True})

    def execute_two_rounds(active_agent, *_args, **_kwargs):
        messages = []
        for index, name in enumerate(
            ("workforce_observe_buzz", "workforce_signal"), start=1
        ):
            call = SimpleNamespace(
                id=f"call-{index}",
                type="function",
                function=SimpleNamespace(name=name, arguments="{}"),
            )
            active_agent._execute_tool_calls_sequential(
                SimpleNamespace(content="", tool_calls=[call]),
                messages,
                "task-ordinary",
            )
        return {"final_response": "ok", "messages": messages, "failed": False}

    with (
        patch("agent.conversation_loop.run_conversation", side_effect=execute_two_rounds),
        patch("run_agent.handle_function_call", side_effect=dispatch),
        patch(
            "hermes_cli.lifecycle.has_hook",
            # An end-only listener still activates the paired host scope.
            side_effect=lambda name: name == "on_turn_end",
        ),
        patch(
            "hermes_cli.lifecycle.invoke_hook",
            side_effect=_invoke_workforce_turn_hook,
        ),
        patch("hermes_cli.observability.relay_shared_metrics.start_task_run"),
        patch("hermes_cli.observability.relay_shared_metrics.finish_task_run"),
    ):
        result = agent.run_conversation("inspect Buzz", task_id="task-ordinary")

    assert result["final_response"] == "ok"
    assert observed["validated"] == observed["dedupe_ref"]
    assert current_buzz_refs() == {}


def test_nested_conversation_claim_gets_isolated_buzz_bindings():
    outer_token, outer = claim_turn()
    try:
        outer_ref, _ = _bind_buzz(event_id="outer")
        inner_token, inner = claim_turn()
        try:
            assert inner is not outer
            assert current_buzz_refs() == {}
            inner_ref, _ = _bind_buzz(event_id="inner", content="Another failure")
            assert inner_ref in current_buzz_refs()
            assert outer_ref not in current_buzz_refs()
        finally:
            release_turn(inner_token, inner)
        assert outer_ref in current_buzz_refs()
        assert inner_ref not in current_buzz_refs()
    finally:
        release_turn(outer_token, outer)


def test_empty_nested_turn_cannot_fall_back_to_parent_observation_cache():
    from tools.workforce_observation_runtime import validate_buzz_signal_binding

    outer_token, outer = claim_turn()
    try:
        outer_ref, outer_evidence = _bind_buzz(event_id="outer-stale")
        inner_token, inner = claim_turn()
        try:
            with pytest.raises(
                ValueError,
                match="not returned by this turn",
            ):
                validate_buzz_signal_binding(
                    dedupe_ref=outer_ref,
                    evidence_references=[outer_evidence],
                )
        finally:
            release_turn(inner_token, inner)
    finally:
        release_turn(outer_token, outer)


def test_cron_state_is_borrowed_and_keeps_host_observed_outcome():
    cron_token, cron_state = activate(True)
    try:
        turn_token, turn_state = claim_turn()
        assert turn_token is None
        assert turn_state is cron_state
        try:
            mark_success()
        finally:
            release_turn(turn_token, turn_state)
        assert cron_state.completed is True
        assert cron_state.turn_claimed is False
    finally:
        reset(cron_token)


def test_abandoned_worker_is_revoked_after_turn_release():
    from tools.workforce_observation_runtime import validate_buzz_signal_binding

    cron_token, cron_state = activate(True)
    turn_token, turn_state = claim_turn()
    dedupe_ref, evidence_ref = _bind_buzz(event_id="abandoned")
    worker = copy_context()
    release_turn(turn_token, turn_state)
    reset(cron_token)

    def late_worker():
        mark_success()
        with pytest.raises(ValueError, match="not returned by this turn"):
            validate_buzz_signal_binding(
                dedupe_ref=dedupe_ref,
                evidence_references=[evidence_ref],
            )

    worker.run(late_worker)
    assert cron_state.completed is False
    assert cron_state.failure is None
    assert cron_state.attempted is False


def test_real_timed_out_executor_worker_cannot_bind_after_turn_end():
    """A worker abandoned by the real timeout path loses signal authority."""
    from run_agent import AIAgent
    from tools.workforce_observation_runtime import validate_buzz_signal_binding

    tool_defs = [{
        "type": "function",
        "function": {
            "name": "workforce_observe_buzz",
            "description": "observe",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    with (
        patch("run_agent.get_tool_definitions", return_value=tool_defs),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("hermes_cli.config.load_config_readonly", return_value={}),
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
    agent.compression_enabled = False
    agent.save_trajectories = False
    release_worker = threading.Event()
    worker_done = threading.Event()
    observed = {}

    def delayed_observation(_name, _args, _task_id, **_kwargs):
        release_worker.wait(timeout=5)
        dedupe_ref, evidence_ref = _bind_buzz(event_id="late-real-worker")
        observed.update(dedupe_ref=dedupe_ref, evidence_ref=evidence_ref)
        try:
            validate_buzz_signal_binding(
                dedupe_ref=dedupe_ref,
                evidence_references=[evidence_ref],
            )
        except ValueError as exc:
            observed["error"] = str(exc)
        finally:
            worker_done.set()
        return json.dumps(observed)

    def execute_timed_out_round(active_agent, *_args, **_kwargs):
        call = SimpleNamespace(
            id="call-timeout",
            type="function",
            function=SimpleNamespace(name="workforce_observe_buzz", arguments="{}"),
        )
        messages = []
        active_agent._execute_tool_calls_sequential(
            SimpleNamespace(content="", tool_calls=[call]),
            messages,
            "task-timeout",
        )
        return {"final_response": "timed out", "messages": messages, "failed": False}

    with (
        patch("agent.conversation_loop.run_conversation", side_effect=execute_timed_out_round),
        patch("run_agent.handle_function_call", side_effect=delayed_observation),
        patch("agent.tool_executor._resolve_sequential_tool_timeout", return_value=0.05),
        patch(
            "hermes_cli.lifecycle.has_hook",
            side_effect=lambda name: name == "on_turn_start",
        ),
        patch(
            "hermes_cli.lifecycle.invoke_hook",
            side_effect=_invoke_workforce_turn_hook,
        ),
        patch("hermes_cli.observability.relay_shared_metrics.start_task_run"),
        patch("hermes_cli.observability.relay_shared_metrics.finish_task_run"),
    ):
        result = agent.run_conversation("inspect Buzz", task_id="task-timeout")

    assert result["final_response"] == "timed out"
    release_worker.set()
    assert worker_done.wait(timeout=5)
    assert observed["error"] == (
        "dedupe_ref was not returned by this turn's Buzz observation"
    )


def test_buzz_binding_without_stable_author_fails_closed():
    events = [{
        "room_id": "room-1", "event_id": "one", "author": "display-only",
        "content": "Service example.service failed",
    }]

    bind_buzz_events(events)

    assert "dedupe_ref" not in events[0]
    assert events[0]["binding_error"] == "stable Buzz event identity unavailable"


def test_signal_is_fixed_nonexecuting_record_for_aurora(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    (profiles / "emily").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profiles / "emily"))
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(SOURCE))
    db_path = tmp_path / "kanban.db"
    monkeypatch.setattr(kanban_db, "kanban_db_path", lambda **_kwargs: db_path)
    result = json.loads(signal._handle(_payload()))
    assert result["success"] is True
    assert result["assignee"] == "aurora"
    assert result["status"] == "blocked"
    assert result["launch_authorized"] is False
    with kanban_db.connect_closing(db_path) as conn:
        task = kanban_db.get_task(conn, result["signal_id"])
        packet = json.loads(task.body)
    assert task.status == "blocked"
    assert packet["decision_owner"] == "aurora"
    assert packet["source_agent"] == "emily"
    assert packet["launch_authorized"] is False


def test_signal_deduplicates_exact_packet(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    (profiles / "main").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profiles / "main"))
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(SOURCE))
    db_path = tmp_path / "kanban.db"
    monkeypatch.setattr(kanban_db, "kanban_db_path", lambda **_kwargs: db_path)
    first = json.loads(signal._handle(_payload()))
    second = json.loads(signal._handle(_payload()))
    assert first["signal_id"] == second["signal_id"]


def test_friend_profile_cannot_submit(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    (profiles / "amy").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profiles / "amy"))
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(SOURCE))
    result = json.loads(signal._handle(_payload()))
    assert result.get("success") is not True
    assert "not eligible" in result["error"]


def test_callers_cannot_choose_launch_or_assignee():
    properties = signal.WORKFORCE_SIGNAL_SCHEMA["parameters"]["properties"]
    assert "assignee" not in properties
    assert "priority" not in properties
    assert "status" not in properties
    assert "launch" not in properties


def test_chloe_can_only_make_mechanical_record_under_aurora_assignment(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    (profiles / "chloe").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profiles / "chloe"))
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(SOURCE))
    db_path = tmp_path / "kanban.db"
    monkeypatch.setattr(kanban_db, "kanban_db_path", lambda **_kwargs: db_path)
    missing_assignment = _payload()
    missing_assignment.pop("department_recommendation")
    denied = json.loads(signal._handle(missing_assignment))
    assert "aurora_assignment_id" in denied["error"]
    with_recommendation = {**_payload(), "aurora_assignment_id": "task-aurora-1"}
    denied = json.loads(signal._handle(with_recommendation))
    assert "may not provide a recommendation" in denied["error"]
    allowed = {
        **missing_assignment,
        "aurora_assignment_id": "task-aurora-1",
    }
    dedupe_ref, evidence_ref = _bind_buzz()
    allowed["dedupe_ref"] = dedupe_ref
    allowed["evidence_references"] = [evidence_ref]
    result = json.loads(signal._handle(allowed))
    assert result["success"] is True
    with kanban_db.connect_closing(db_path) as conn:
        packet = json.loads(kanban_db.get_task(conn, result["signal_id"]).body)
    assert packet["source_agent"] == "chloe"
    assert packet["aurora_assignment_id"] == "task-aurora-1"
    assert packet["launch_authorized"] is False


def test_chloe_offline_write_failure_is_observed_and_a_later_retry_can_recover(tmp_path, monkeypatch):
    """Each Cron attempt gets fresh host state; a prior outage cannot go green."""
    profiles = tmp_path / "profiles"
    (profiles / "chloe").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profiles / "chloe"))
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(SOURCE))
    payload = {
        **_payload(),
        "department_recommendation": "",
        "aurora_assignment_id": "t_aurora-1",
    }
    token, failed_attempt = activate(True)
    try:
        dedupe_ref, evidence_ref = _bind_buzz()
        payload["dedupe_ref"] = dedupe_ref
        payload["evidence_references"] = [evidence_ref]
        monkeypatch.setattr(signal, "record_signal", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
        failure = json.loads(signal._handle(payload))
    finally:
        reset(token)
    assert failure.get("success") is not True
    assert failed_attempt.failure == "offline"
    assert failed_attempt.completed is False

    db_path = tmp_path / "kanban.db"
    monkeypatch.setattr(kanban_db, "kanban_db_path", lambda **_kwargs: db_path)
    monkeypatch.undo()
    monkeypatch.setenv("HERMES_HOME", str(profiles / "chloe"))
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(SOURCE))
    token, recovered_attempt = activate(True)
    try:
        dedupe_ref, evidence_ref = _bind_buzz()
        payload["dedupe_ref"] = dedupe_ref
        payload["evidence_references"] = [evidence_ref]
        recovery = json.loads(signal._handle(payload))
    finally:
        reset(token)
    assert recovery["success"] is True
    assert recovered_attempt.failure is None
    assert recovered_attempt.completed is True


def test_chloe_reobserves_same_buzz_fact_despite_model_wording_drift(
    tmp_path, monkeypatch
):
    profiles = tmp_path / "profiles"
    (profiles / "chloe").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profiles / "chloe"))
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(SOURCE))
    db_path = tmp_path / "kanban.db"
    monkeypatch.setattr(kanban_db, "kanban_db_path", lambda **_kwargs: db_path)
    content = (
        "Maggie finance service failure\n\n"
        "Service elliott-finance-qbo-refresh.service failed."
    )

    first_ref, first_evidence = _bind_buzz(
        event_id="qbo-event-1", content=content
    )
    first_payload = {
        **_payload(),
        "department_recommendation": "",
        "aurora_assignment_id": "workflow:test:chloe",
        "dedupe_ref": first_ref,
        "evidence_references": [first_evidence],
        "expected_outcome": "Restore the finance refresh service",
        "action_class": "risk",
    }
    first = json.loads(signal._handle(first_payload))

    second_ref, second_evidence = _bind_buzz(
        event_id="qbo-event-2", content=content
    )
    second_payload = {
        **first_payload,
        "dedupe_ref": second_ref,
        "evidence_references": [second_evidence],
        "expected_outcome": "Keep the current finance failure visible",
        "observation": "The exact alert recurred in the later window",
        "action_class": "exception",
        "target_ref": "systemd:elliott-finance-qbo-refresh.service",
    }
    second = json.loads(signal._handle(second_payload))

    assert first_ref == second_ref
    assert first["signal_id"] == second["signal_id"]
    assert first["created"] is True
    assert second["created"] is False
    with kanban_db.connect_closing(db_path) as conn:
        item = conn.execute(
            "SELECT evidence_json,provenance_json FROM wc_items WHERE task_id=?",
            (first["signal_id"],),
        ).fetchone()
        task = kanban_db.get_task(conn, first["signal_id"])
    assert json.loads(item["evidence_json"]) == [first_evidence, second_evidence]
    assert len(json.loads(item["provenance_json"])) == 2
    body = json.loads(task.body)
    assert body["observation"] == second_payload["observation"]
    assert body["latest_source_agent"] == "chloe"
    assert body["evidence_references"] == [first_evidence, second_evidence]


def test_chloe_rejects_missing_stale_and_mismatched_buzz_binding(
    tmp_path, monkeypatch
):
    profiles = tmp_path / "profiles"
    (profiles / "chloe").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profiles / "chloe"))
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(SOURCE))
    db_path = tmp_path / "kanban.db"
    monkeypatch.setattr(kanban_db, "kanban_db_path", lambda **_kwargs: db_path)
    base = {
        **_payload(),
        "department_recommendation": "",
        "aurora_assignment_id": "workflow:test:chloe",
    }

    missing = json.loads(signal._handle(base))
    assert "dedupe_ref is required" in missing["error"]
    dedupe_ref, evidence_ref = _bind_buzz()
    stale = json.loads(signal._handle({
        **base,
        "dedupe_ref": "buzz-content:" + "0" * 64,
        "evidence_references": [evidence_ref],
    }))
    assert "not returned by this turn" in stale["error"]
    mismatched = json.loads(signal._handle({
        **base,
        "dedupe_ref": dedupe_ref,
        "evidence_references": ["buzz:event:another-event"],
    }))
    assert "outside the selected dedupe_ref" in mismatched["error"]
    valid_plus_forged = json.loads(signal._handle({
        **base,
        "dedupe_ref": dedupe_ref,
        "evidence_references": [evidence_ref, "buzz:event:forged"],
    }))
    assert "outside the selected dedupe_ref" in valid_plus_forged["error"]
    assert not db_path.exists()


def test_mel_cannot_route_a_signal(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    (profiles / "mel").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profiles / "mel"))
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(SOURCE))
    result = json.loads(signal._handle(_payload()))
    assert result.get("success") is not True
    assert "may not route" in result["error"]


def test_signal_uses_task_profile_override_before_process_environment(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    (profiles / "amy").mkdir(parents=True)
    (profiles / "emily").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profiles / "amy"))
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(SOURCE))
    db_path = tmp_path / "kanban.db"
    monkeypatch.setattr(kanban_db, "kanban_db_path", lambda **_kwargs: db_path)

    token = set_hermes_home_override(profiles / "emily")
    try:
        result = json.loads(signal._handle(_payload()))
    finally:
        reset_hermes_home_override(token)

    assert result["success"] is True
    assert result["source_agent"] == "emily"
