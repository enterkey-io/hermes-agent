from contextvars import copy_context
import hashlib
import json
from pathlib import Path

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import kanban_db
from tools import workforce_signal_tool as signal
from tools.workforce_observation_runtime import bind_buzz_events
from tools.workforce_signal_runtime import activate, reset


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
        "content": content,
    }]
    bind_buzz_events(events)
    return events[0]["dedupe_ref"], events[0]["evidence_ref"]


def test_buzz_binding_distinguishes_full_messages_with_same_display_prefix():
    prefix = "x" * 600
    first_full = prefix + " first"
    second_full = prefix + " second"
    first = [{
        "room_id": "room-1", "event_id": "one", "content": prefix,
        "_full_content_sha256": hashlib.sha256(first_full.encode()).hexdigest(),
    }]
    second = [{
        "room_id": "room-1", "event_id": "two", "content": prefix,
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
        "room_id": "room-1", "event_id": "one", "author": "notifier-a",
        "content": "Service example.service failed",
    }]
    second = [{
        "room_id": "room-1", "event_id": "two", "author": "human-copy",
        "content": "Service example.service failed",
    }]
    bind_buzz_events(first)
    bind_buzz_events(second)
    assert first[0]["dedupe_ref"] != second[0]["dedupe_ref"]


def test_buzz_binding_survives_tool_worker_context_copy():
    token, state = activate(False, observe_attempts=True)
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
    finally:
        reset(token)


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
    dedupe_ref, evidence_ref = _bind_buzz()
    payload["dedupe_ref"] = dedupe_ref
    payload["evidence_references"] = [evidence_ref]
    token, failed_attempt = activate(True)
    try:
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
    assert "must include" in mismatched["error"]
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
