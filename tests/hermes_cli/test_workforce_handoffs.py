from pathlib import Path
import json
import time

import pytest

from hermes_cli import kanban_db
from hermes_cli.workforce_handoffs import (
    acknowledge_handoff,
    claim_owned_failure_handoff_pickup,
    claim_workforce_handoff_pickup,
    create_handoff,
    record_checkpoint,
    sweep_overdue_handoffs,
)
from hermes_cli.workforce_org import load_organization


ORG = load_organization(Path(__file__).parents[2] / "workforce" / "organization.yaml")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with kanban_db.connect_closing() as connection:
        yield connection


def _iso(epoch: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def test_cross_director_handoff_requires_aurora(conn):
    now = int(time.time())
    with pytest.raises(ValueError, match="route through Aurora"):
        create_handoff(
            conn,
            source_agent="emily",
            target_agent="xenia",
            expected_outcome="Validate data",
            acceptance_test="Evidence attached",
            evidence_references=[],
            acknowledgment_deadline=_iso(now + 60),
            checkpoint_at=_iso(now + 120),
            organization=ORG,
        )


@pytest.mark.parametrize(
    ("source", "target"),
    [("milena", "grace"), ("emily", "aurora"), ("sage", "emily"), ("grace", "aurora")],
)
def test_reporting_line_and_executive_peer_handoffs_route_internally(conn, source, target):
    now = int(time.time())
    created = create_handoff(
        conn,
        source_agent=source,
        target_agent=target,
        expected_outcome="Resolve one internal dependency",
        acceptance_test="The accountable manager records a disposition",
        evidence_references=["kanban:t_source"],
        acknowledgment_deadline=_iso(now + 60),
        checkpoint_at=_iso(now + 120),
        organization=ORG,
    )
    assert created["source_agent"] == source
    assert created["target_agent"] == target


def test_exact_create_retry_returns_persisted_handoff_without_duplicate(conn):
    now = int(time.time())
    kwargs = {
        "source_agent": "alina",
        "target_agent": "aurora",
        "expected_outcome": "Own one routed host decision",
        "acceptance_test": "Aurora records the evidence-backed disposition",
        "evidence_references": ["kanban:t_source"],
        "acknowledgment_deadline": _iso(now + 60),
        "checkpoint_at": _iso(now + 120),
        "organization": ORG,
        "session_id": "origin-session",
        "coordination_origin_message_id": "origin-message",
    }

    first = create_handoff(conn, **kwargs)
    retry = create_handoff(conn, **kwargs)

    assert first["created"] is True
    assert retry["created"] is False
    assert retry == {**first, "created": False}
    assert retry["creation_binding"].startswith("sha256:")
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert [
        event.kind for event in kanban_db.list_events(conn, first["task_id"])
    ] == ["created", "workforce_handoff_created"]


@pytest.mark.parametrize(
    ("session_id", "origin_message_id", "acceptance_test"),
    [
        ("other-session", "origin-message", "Aurora records the disposition"),
        ("origin-session", "other-message", "Aurora records the disposition"),
        ("origin-session", "origin-message", "A changed acceptance contract"),
    ],
)
def test_create_retry_rejects_changed_identity_or_origin(
    conn, session_id, origin_message_id, acceptance_test,
):
    now = int(time.time())
    base = {
        "source_agent": "alina",
        "target_agent": "aurora",
        "expected_outcome": "Own one routed host decision",
        "acceptance_test": "Aurora records the disposition",
        "evidence_references": ["kanban:t_source"],
        "acknowledgment_deadline": _iso(now + 60),
        "checkpoint_at": _iso(now + 120),
        "organization": ORG,
        "session_id": "origin-session",
        "coordination_origin_message_id": "origin-message",
    }
    created = create_handoff(conn, **base)

    with pytest.raises(ValueError, match="different workforce handoff or origin"):
        create_handoff(
            conn,
            **{
                **base,
                "session_id": session_id,
                "coordination_origin_message_id": origin_message_id,
                "acceptance_test": acceptance_test,
            },
        )

    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    task = kanban_db.get_task(conn, created["task_id"])
    assert task is not None
    assert task.session_id == "origin-session"


def test_owned_failure_context_requires_literal_source_acceptance(conn):
    now = int(time.time())
    with pytest.raises(ValueError, match="require source acceptance"):
        create_handoff(
            conn,
            source_agent="aurora",
            target_agent="alina",
            expected_outcome="Repair the owned operational failure",
            acceptance_test="A later execution succeeds",
            evidence_references=["execution:failure"],
            acknowledgment_deadline=_iso(now + 60),
            checkpoint_at=_iso(now + 120),
            organization=ORG,
            context={
                "kind": "owned_operational_failure",
                "technical_owner": "alina",
                "director": "aurora",
            },
            requires_source_acceptance=False,
        )

    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_owned_failure_handoff_rejects_inherited_request_atomically(conn):
    root_id = kanban_db.create_task(
        conn,
        title="Accepted user request",
        assignee="aurora",
        session_id="origin-session",
    )
    kanban_db.add_notify_sub(
        conn,
        task_id=root_id,
        platform="telegram",
        chat_id="origin-chat",
        notifier_profile="aurora",
        delivery_mode="wake",
    )
    request = kanban_db.create_coordination_request(
        conn,
        root_task_id=root_id,
        origin_session_id="origin-session",
        origin_message_id="origin-message",
        organization=ORG,
    )
    now = int(time.time())

    with pytest.raises(ValueError, match="cannot inherit"):
        create_handoff(
            conn,
            source_agent="aurora",
            target_agent="alina",
            expected_outcome="Repair a separate operational failure",
            acceptance_test="Two later executions succeed",
            evidence_references=["execution:failure-1"],
            acknowledgment_deadline=_iso(now + 60),
            checkpoint_at=_iso(now + 120),
            organization=ORG,
            context={
                "kind": "owned_operational_failure",
                "technical_owner": "alina",
                "director": "aurora",
                "workflow_id": "separate-integration",
                "event_id": "failure-1",
            },
            requires_source_acceptance=True,
            coordination_source_task_id=root_id,
            session_id=request.origin_session_id,
            coordination_origin_message_id=request.origin_message_id,
        )

    assert conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE id != ?",
        (root_id,),
    ).fetchone()[0] == 0


def test_ordinary_handoff_pickup_is_one_shot_without_fabricating_request(conn):
    now = int(time.time())
    created = create_handoff(
        conn,
        source_agent="alina",
        target_agent="aurora",
        expected_outcome="Own the routed host decision",
        acceptance_test="Aurora records a verified disposition",
        evidence_references=["kanban:t_source"],
        acknowledgment_deadline=_iso(now + 60),
        checkpoint_at=_iso(now + 120),
        organization=ORG,
    )

    pickup = claim_workforce_handoff_pickup(
        conn, target_agent="aurora", organization=ORG, now=now + 1,
    )
    duplicate = claim_workforce_handoff_pickup(
        conn, target_agent="aurora", organization=ORG, now=now + 2,
    )

    assert pickup == {
        "task_id": created["task_id"],
        "target_agent": "aurora",
        "source_agent": "alina",
        "request_root_id": None,
        "claim_kind": "ordinary",
        "claimed_at": now + 1,
    }
    assert duplicate is None
    assert conn.execute(
        "SELECT COUNT(*) FROM coordination_requests"
    ).fetchone()[0] == 0
    assert kanban_db.get_task(conn, created["task_id"]).status == "triage"

    acknowledge_handoff(
        conn,
        created["task_id"],
        actor="aurora",
        organization=ORG,
        now=now + 3,
    )
    task = kanban_db.get_task(conn, created["task_id"])
    assert task.status == "ready"
    assert json.loads(task.body)["state"] == "accepted"


def test_ordinary_pickup_rejects_a_contract_changed_after_creation(conn):
    now = int(time.time())
    created = create_handoff(
        conn,
        source_agent="alina",
        target_agent="aurora",
        expected_outcome="Own the routed host decision",
        acceptance_test="Aurora records a verified disposition",
        evidence_references=["kanban:t_source"],
        acknowledgment_deadline=_iso(now + 60),
        checkpoint_at=_iso(now + 120),
        organization=ORG,
    )
    task = kanban_db.get_task(conn, created["task_id"])
    changed = json.loads(task.body)
    changed["acceptance_test"] = "A silently substituted acceptance contract"
    with kanban_db.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET body = ? WHERE id = ?",
            (json.dumps(changed), task.id),
        )

    assert claim_workforce_handoff_pickup(
        conn, target_agent="aurora", organization=ORG, now=now + 1,
    ) is None
    with pytest.raises(ValueError, match="contract changed"):
        acknowledge_handoff(
            conn,
            task.id,
            actor="aurora",
            organization=ORG,
            now=now + 2,
        )
    assert all(
        event.kind != "workforce_handoff_pickup_claimed"
        for event in kanban_db.list_events(conn, task.id)
    )


def test_ordinary_pickup_does_not_activate_legacy_backlog_rows(conn):
    now = int(time.time())
    legacy_id = kanban_db.create_task(
        conn,
        title="Historical ordinary handoff",
        body=json.dumps({
            "kind": "workforce_handoff",
            "state": "pending_acknowledgment",
            "source_agent": "alina",
            "target_agent": "aurora",
            "acknowledgment_deadline": now + 60,
            "checkpoint_at": now + 120,
            "requires_source_acceptance": False,
        }),
        assignee="aurora",
        created_by="alina",
        triage=True,
    )
    forged_id = kanban_db.create_task(
        conn,
        title="Unprovenanced ordinary handoff",
        body=json.dumps({
            "kind": "workforce_handoff",
            "delivery_contract_version": 1,
            "creation_binding": "sha256:not-authoritative",
            "state": "pending_acknowledgment",
            "source_agent": "alina",
            "target_agent": "aurora",
            "acknowledgment_deadline": now + 60,
            "checkpoint_at": now + 120,
            "requires_source_acceptance": False,
        }),
        assignee="aurora",
        created_by="alina",
        triage=True,
    )

    assert claim_workforce_handoff_pickup(
        conn, target_agent="aurora", organization=ORG, now=now + 1,
    ) is None
    assert all(
        event.kind != "workforce_handoff_pickup_claimed"
        for task_id in (legacy_id, forged_id)
        for event in kanban_db.list_events(conn, task_id)
    )
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    assert kanban_db.has_coordination_tick_work(
        db_path,
        notifier_agents={"aurora"},
        notifier_profiles={"aurora"},
    ) is False


def test_ordinary_handoff_inherits_existing_request_from_trusted_source(
    conn, monkeypatch,
):
    monkeypatch.setenv(
        "HERMES_WORKFORCE_ORG",
        str(Path(__file__).parents[2] / "workforce" / "organization.yaml"),
    )
    root_id = kanban_db.create_task(
        conn,
        title="Return the accepted request",
        assignee="aurora",
        session_id="origin-session",
    )
    kanban_db.add_notify_sub(
        conn,
        task_id=root_id,
        platform="telegram",
        chat_id="origin-chat",
        notifier_profile="aurora",
        delivery_mode="wake",
    )
    request = kanban_db.create_coordination_request(
        conn,
        root_task_id=root_id,
        origin_session_id="origin-session",
        origin_message_id="origin-message",
        organization=ORG,
    )
    now = int(time.time())

    created = create_handoff(
        conn,
        source_agent="aurora",
        target_agent="alina",
        expected_outcome="Repair one host issue",
        acceptance_test="The repair has full-path evidence",
        evidence_references=["kanban:t_source"],
        acknowledgment_deadline=_iso(now + 60),
        checkpoint_at=_iso(now + 120),
        organization=ORG,
        coordination_source_task_id=root_id,
        session_id="origin-session",
        coordination_origin_message_id="origin-message",
    )

    task = kanban_db.get_task(conn, created["task_id"])
    assert task.request_root_id == request.id
    assert task.session_id == "origin-session"
    event = kanban_db.list_events(conn, task.id)[0]
    assert event.payload["request_root_id"] == request.id
    assert event.payload["coordination_origin_message_id"] == "origin-message"


@pytest.mark.parametrize("boundary", ["return_pending", "checkpoint"])
def test_linked_handoff_cannot_acknowledge_after_request_closes(
    conn, boundary, monkeypatch,
):
    monkeypatch.setenv(
        "HERMES_WORKFORCE_ORG",
        str(Path(__file__).parents[2] / "workforce" / "organization.yaml"),
    )
    now = int(time.time())
    root_id = kanban_db.create_task(
        conn,
        title="Return the accepted request",
        assignee="aurora",
        session_id="origin-session",
    )
    kanban_db.add_notify_sub(
        conn,
        task_id=root_id,
        platform="telegram",
        chat_id="origin-chat",
        notifier_profile="aurora",
        delivery_mode="wake",
    )
    request = kanban_db.create_coordination_request(
        conn,
        root_task_id=root_id,
        origin_session_id="origin-session",
        origin_message_id="origin-message",
        checkpoint_seconds=20,
        organization=ORG,
        now=now,
    )
    created = create_handoff(
        conn,
        source_agent="aurora",
        target_agent="alina",
        expected_outcome="Repair one host issue",
        acceptance_test="The repair has full-path evidence",
        evidence_references=["kanban:t_source"],
        acknowledgment_deadline=_iso(now + 60),
        checkpoint_at=_iso(now + 120),
        organization=ORG,
        coordination_source_task_id=root_id,
        session_id="origin-session",
        coordination_origin_message_id="origin-message",
    )
    pickup = claim_workforce_handoff_pickup(
        conn, target_agent="alina", organization=ORG, now=now + 1,
    )
    assert pickup is not None
    accepted_at = now + 2
    if boundary == "return_pending":
        with kanban_db.write_txn(conn):
            conn.execute(
                "UPDATE coordination_requests SET status = 'return_pending' "
                "WHERE id = ?",
                (request.id,),
            )
    else:
        accepted_at = request.checkpoint_at

    with pytest.raises(ValueError, match="no longer active"):
        acknowledge_handoff(
            conn,
            created["task_id"],
            actor="alina",
            organization=ORG,
            now=accepted_at,
        )

    task = kanban_db.get_task(conn, created["task_id"])
    assert task.status == "triage"
    assert json.loads(task.body)["state"] == "pending_acknowledgment"
    assert "workforce_handoff_acknowledged" not in {
        event.kind for event in kanban_db.list_events(conn, task.id)
    }


def test_ordinary_handoff_inside_owned_failure_request_is_visible_and_claimed(
    conn, monkeypatch,
):
    monkeypatch.setenv(
        "HERMES_WORKFORCE_ORG",
        str(Path(__file__).parents[2] / "workforce" / "organization.yaml"),
    )
    now = int(time.time())
    owned_root = create_handoff(
        conn,
        source_agent="aurora",
        target_agent="alina",
        expected_outcome="Repair the owned operational failure",
        acceptance_test="Two later executions succeed",
        evidence_references=["execution:failure-1"],
        acknowledgment_deadline=_iso(now + 60),
        checkpoint_at=_iso(now + 3600),
        organization=ORG,
        context={
            "kind": "owned_operational_failure",
            "technical_owner": "alina",
            "director": "aurora",
            "workflow_id": "scheduled-integration",
            "event_id": "failure-1",
        },
        requires_source_acceptance=True,
    )
    root_pickup = claim_workforce_handoff_pickup(
        conn,
        target_agent="alina",
        organization=ORG,
        now=now + 1,
    )
    assert root_pickup is not None
    request = kanban_db.get_coordination_request(
        conn, root_pickup["request_root_id"]
    )
    assert request is not None
    assert request.kind == "owned_operational_failure"

    child = create_handoff(
        conn,
        source_agent="alina",
        target_agent="aurora",
        expected_outcome="Decide the bounded host repair",
        acceptance_test="The decision is recorded with evidence",
        evidence_references=[f"kanban:{owned_root['task_id']}"],
        acknowledgment_deadline=_iso(now + 120),
        checkpoint_at=_iso(now + 1800),
        organization=ORG,
        coordination_source_task_id=owned_root["task_id"],
        session_id=request.origin_session_id,
        coordination_origin_message_id=request.origin_message_id,
    )
    child_task = kanban_db.get_task(conn, child["task_id"])
    assert child_task is not None
    assert child_task.request_root_id == request.id
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    assert kanban_db.has_coordination_tick_work(
        db_path,
        notifier_agents={"aurora"},
        notifier_profiles={"aurora"},
    )

    child_pickup = claim_workforce_handoff_pickup(
        conn,
        target_agent="aurora",
        organization=ORG,
        now=now + 2,
    )

    assert child_pickup == {
        "task_id": child["task_id"],
        "target_agent": "aurora",
        "source_agent": "alina",
        "request_root_id": request.id,
        "claim_kind": "ordinary",
        "claimed_at": now + 2,
    }
    assert claim_workforce_handoff_pickup(
        conn,
        target_agent="aurora",
        organization=ORG,
        now=now + 3,
    ) is None


def test_ordinary_handoff_rejects_unknown_inherited_request_kind(conn):
    now = int(time.time())
    root_id = kanban_db.create_task(
        conn,
        title="Unsupported coordination root",
        assignee="alina",
        session_id="unsupported-session",
    )
    kanban_db.add_notify_sub(
        conn,
        task_id=root_id,
        platform="telegram",
        chat_id="unsupported-chat",
        notifier_profile="alina",
        delivery_mode="wake",
    )
    request = kanban_db.create_coordination_request(
        conn,
        root_task_id=root_id,
        origin_session_id="unsupported-session",
        origin_message_id="unsupported-message",
        organization=ORG,
    )
    with kanban_db.write_txn(conn):
        conn.execute(
            "UPDATE coordination_requests SET kind = 'future_unknown' WHERE id = ?",
            (request.id,),
        )

    with pytest.raises(ValueError, match="unsupported"):
        create_handoff(
            conn,
            source_agent="alina",
            target_agent="aurora",
            expected_outcome="Handle unsupported nested work",
            acceptance_test="The work is never stranded",
            evidence_references=[f"kanban:{root_id}"],
            acknowledgment_deadline=_iso(now + 60),
            checkpoint_at=_iso(now + 120),
            organization=ORG,
            coordination_source_task_id=root_id,
            session_id="unsupported-session",
            coordination_origin_message_id="unsupported-message",
        )

    assert conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE id != ?",
        (root_id,),
    ).fetchone()[0] == 0


def test_coordinated_handoff_rejects_route_mismatch_and_inactive_pickup(
    conn, monkeypatch,
):
    monkeypatch.setenv(
        "HERMES_WORKFORCE_ORG",
        str(Path(__file__).parents[2] / "workforce" / "organization.yaml"),
    )
    root_id = kanban_db.create_task(
        conn,
        title="Return the accepted request",
        assignee="aurora",
        session_id="origin-session",
    )
    kanban_db.add_notify_sub(
        conn,
        task_id=root_id,
        platform="telegram",
        chat_id="origin-chat",
        notifier_profile="aurora",
        delivery_mode="wake",
    )
    request = kanban_db.create_coordination_request(
        conn,
        root_task_id=root_id,
        origin_session_id="origin-session",
        origin_message_id="origin-message",
        organization=ORG,
    )
    now = int(time.time())
    kwargs = {
        "source_agent": "aurora",
        "target_agent": "alina",
        "expected_outcome": "Repair one host issue",
        "acceptance_test": "The repair has full-path evidence",
        "evidence_references": ["kanban:t_source"],
        "acknowledgment_deadline": _iso(now + 60),
        "checkpoint_at": _iso(now + 120),
        "organization": ORG,
        "coordination_source_task_id": root_id,
        "session_id": "origin-session",
        "coordination_origin_message_id": "origin-message",
    }

    with pytest.raises(ValueError, match="origin must match"):
        create_handoff(
            conn,
            **{**kwargs, "coordination_origin_message_id": "wrong-message"},
        )
    created = create_handoff(conn, **kwargs)
    with kanban_db.write_txn(conn):
        conn.execute(
            "UPDATE coordination_requests SET status = 'completed' WHERE id = ?",
            (request.id,),
        )

    assert claim_workforce_handoff_pickup(
        conn, target_agent="alina", organization=ORG, now=now + 1,
    ) is None
    assert all(
        event.kind != "workforce_handoff_pickup_claimed"
        for event in kanban_db.list_events(conn, created["task_id"])
    )
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    assert kanban_db.has_coordination_tick_work(
        db_path,
        notifier_agents={"alina"},
        notifier_profiles={"alina"},
    ) is False


def test_receiver_must_acknowledge_and_stalled_checkpoint_notifies_aurora_chloe(conn):
    now = int(time.time())
    created = create_handoff(
        conn,
        source_agent="aurora",
        target_agent="emily",
        expected_outcome="Prepare product evidence",
        acceptance_test="Packet contains source links",
        evidence_references=["kanban:source"],
        acknowledgment_deadline=_iso(now + 60),
        checkpoint_at=_iso(now + 120),
        organization=ORG,
    )
    task_id = created["task_id"]
    assert kanban_db.get_task(conn, task_id).status == "triage"
    with pytest.raises(ValueError, match="receiving agent"):
        acknowledge_handoff(conn, task_id, actor="xenia", organization=ORG, now=now + 10)
    accepted = acknowledge_handoff(
        conn, task_id, actor="emily", organization=ORG, now=now + 10
    )
    assert accepted["state"] == "accepted"
    assert kanban_db.get_task(conn, task_id).status == "ready"
    stalled = sweep_overdue_handoffs(
        conn, actor="chloe", organization=ORG, now=now + 121
    )
    assert stalled == [{
        "task_id": task_id,
        "state": "stalled",
        "notify": ["aurora", "chloe"],
        "decision_owner": "aurora",
    }]
    blocked = kanban_db.get_task(conn, task_id)
    assert blocked.status == "blocked"
    assert blocked.block_kind == "capability"
    assert [event.kind for event in kanban_db.list_events(conn, task_id)][-2:] == [
        "workforce_handoff_stalled",
        "blocked",
    ]


def test_checkpoint_moves_deadline_without_changing_authority(conn):
    now = int(time.time())
    created = create_handoff(
        conn,
        source_agent="emily",
        target_agent="sage",
        expected_outcome="Review product evidence",
        acceptance_test="Findings linked",
        evidence_references=[],
        acknowledgment_deadline=_iso(now + 60),
        checkpoint_at=_iso(now + 120),
        organization=ORG,
    )
    acknowledge_handoff(
        conn, created["task_id"], actor="sage", organization=ORG, now=now + 10
    )
    result = record_checkpoint(
        conn,
        created["task_id"],
        actor="sage",
        evidence_references=["repo:commit"],
        next_checkpoint_at=_iso(now + 240),
        organization=ORG,
        now=now + 100,
    )
    assert result["state"] == "active"
    assert result["checkpoint_at"] == now + 240


def test_owned_failure_pickup_is_one_shot_bounded_and_keeps_ack_pending(conn):
    now = int(time.time())
    created = create_handoff(
        conn,
        source_agent="aurora",
        target_agent="alina",
        expected_outcome="Repair scheduled integration",
        acceptance_test="Two later executions succeed",
        evidence_references=["execution:failure-1"],
        acknowledgment_deadline=_iso(now + 60),
        checkpoint_at=_iso(now + 3600),
        organization=ORG,
        context={
            "kind": "owned_operational_failure",
            "technical_owner": "alina",
            "director": "aurora",
            "workflow_id": "scheduled-integration",
            "event_id": "failure-1",
        },
        requires_source_acceptance=True,
    )
    task_id = created["task_id"]

    pickup = claim_owned_failure_handoff_pickup(
        conn, target_agent="alina", organization=ORG, now=now + 1,
    )
    duplicate = claim_owned_failure_handoff_pickup(
        conn, target_agent="alina", organization=ORG, now=now + 2,
    )

    assert pickup == {
        "task_id": task_id,
        "target_agent": "alina",
        "source_agent": "aurora",
        "request_root_id": pickup["request_root_id"],
        "claimed_at": now + 1,
    }
    assert duplicate is None
    task = kanban_db.get_task(conn, task_id)
    assert task.status == "triage"
    assert task.request_root_id == pickup["request_root_id"]
    assert json.loads(task.body)["state"] == "pending_acknowledgment"
    assert conn.execute(
        "SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?", (task_id,)
    ).fetchone()[0] == 0
    request = kanban_db.get_coordination_request(
        conn, pickup["request_root_id"]
    )
    assert request.kind == "owned_operational_failure"
    assert request.max_model_calls == 20
    assert request.final_model_call_reserve == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
        "AND kind = 'workforce_handoff_pickup_claimed'",
        (task_id,),
    ).fetchone()[0] == 1

    accepted = acknowledge_handoff(
        conn, task_id, actor="alina", organization=ORG, now=now + 3,
    )
    assert accepted["state"] == "accepted"
    assert kanban_db.get_task(conn, task_id).status == "ready"


def test_pickup_ignores_generic_and_expired_handoffs(conn):
    now = int(time.time())
    create_handoff(
        conn,
        source_agent="aurora",
        target_agent="alina",
        expected_outcome="Ordinary handoff",
        acceptance_test="Done",
        evidence_references=[],
        acknowledgment_deadline=_iso(now + 60),
        checkpoint_at=_iso(now + 120),
        organization=ORG,
    )
    create_handoff(
        conn,
        source_agent="aurora",
        target_agent="alina",
        expected_outcome="Expired operational failure",
        acceptance_test="Recovered",
        evidence_references=[],
        acknowledgment_deadline=_iso(now - 10),
        checkpoint_at=_iso(now + 120),
        organization=ORG,
        context={
            "kind": "owned_operational_failure",
            "technical_owner": "alina",
            "director": "aurora",
        },
        requires_source_acceptance=True,
        allow_overdue=True,
    )
    assert claim_owned_failure_handoff_pickup(
        conn, target_agent="alina", organization=ORG, now=now,
    ) is None


def test_pickup_skips_malformed_first_candidate_and_requires_literal_flag(conn):
    now = int(time.time())
    malformed_body = {
        "kind": "workforce_handoff",
        "state": "pending_acknowledgment",
        "source_agent": "aurora",
        "target_agent": "alina",
        "acknowledgment_deadline": "not-an-integer",
        "requires_source_acceptance": True,
        "context": {
            "kind": "owned_operational_failure",
            "technical_owner": "alina",
            "director": "aurora",
        },
    }
    malformed = kanban_db.create_task(
        conn,
        title="Malformed first candidate",
        body=json.dumps(malformed_body),
        assignee="alina",
        triage=True,
    )
    truthy_flag_body = dict(malformed_body)
    truthy_flag_body["acknowledgment_deadline"] = now + 60
    truthy_flag_body["requires_source_acceptance"] = "true"
    truthy = kanban_db.create_task(
        conn,
        title="Truthy flag is not authority",
        body=json.dumps(truthy_flag_body),
        assignee="alina",
        triage=True,
    )
    malformed_checkpoint_body = dict(malformed_body)
    malformed_checkpoint_body["acknowledgment_deadline"] = now + 60
    malformed_checkpoint_body["checkpoint_at"] = "not-an-integer"
    malformed_checkpoint = kanban_db.create_task(
        conn,
        title="Malformed checkpoint",
        body=json.dumps(malformed_checkpoint_body),
        assignee="alina",
        triage=True,
    )
    invalid_factory_body = dict(malformed_body)
    invalid_factory_body["acknowledgment_deadline"] = now + 60
    invalid_factory_body["checkpoint_at"] = now + 120
    invalid_factory_body["context"] = {
        **malformed_body["context"],
        "technical_owner": "sage",
    }
    invalid_factory = kanban_db.create_task(
        conn,
        title="Factory-invalid context",
        body=json.dumps(invalid_factory_body),
        assignee="alina",
        triage=True,
    )
    valid = create_handoff(
        conn,
        source_agent="aurora",
        target_agent="alina",
        expected_outcome="Repair valid later candidate",
        acceptance_test="Two later executions succeed",
        evidence_references=["execution:failure-valid"],
        acknowledgment_deadline=_iso(now + 60),
        checkpoint_at=_iso(now + 3600),
        organization=ORG,
        context={
            "kind": "owned_operational_failure",
            "technical_owner": "alina",
            "director": "aurora",
            "event_id": "failure-valid",
        },
        requires_source_acceptance=True,
    )["task_id"]
    with kanban_db.write_txn(conn):
        for created_at, task_id in enumerate(
            (malformed, truthy, malformed_checkpoint, invalid_factory, valid),
            start=1,
        ):
            conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id = ?",
                (created_at, task_id),
            )

    pickup = claim_owned_failure_handoff_pickup(
        conn, target_agent="alina", organization=ORG, now=now,
    )

    assert pickup is not None
    assert pickup["task_id"] == valid
    assert kanban_db.get_task(conn, malformed).request_root_id is None
    assert kanban_db.get_task(conn, truthy).request_root_id is None
    assert kanban_db.get_task(conn, malformed_checkpoint).request_root_id is None
    assert kanban_db.get_task(conn, invalid_factory).request_root_id is None


def test_owned_failure_review_wait_is_not_marked_stalled(conn):
    now = int(time.time())
    task_id = create_handoff(
        conn,
        source_agent="aurora",
        target_agent="alina",
        expected_outcome="Repair and wait for recovery proof",
        acceptance_test="Two later executions succeed",
        evidence_references=["execution:failure-1"],
        acknowledgment_deadline=_iso(now + 60),
        checkpoint_at=_iso(now + 120),
        organization=ORG,
        context={
            "kind": "owned_operational_failure",
            "technical_owner": "alina",
            "director": "aurora",
            "event_id": "failure-1",
        },
        requires_source_acceptance=True,
    )["task_id"]
    claim_owned_failure_handoff_pickup(
        conn, target_agent="alina", organization=ORG, now=now + 1,
    )
    acknowledge_handoff(
        conn, task_id, actor="alina", organization=ORG, now=now + 2,
    )
    owner_run = kanban_db.claim_task(conn, task_id, claimer="alina:test")
    assert owner_run is not None
    assert kanban_db.request_review(
        conn,
        task_id,
        summary="repair applied",
        expected_run_id=owner_run.current_run_id,
    )
    with kanban_db.write_txn(conn):
        kanban_db._append_event(
            conn,
            task_id,
            "workforce_handoff_recovery_required",
            {
                "failure_event_id": "failure-1",
                "failure_order": 10,
                "required_successes": 2,
            },
        )

    assert sweep_overdue_handoffs(
        conn, actor="chloe", organization=ORG, now=now + 86_400,
    ) == []
    assert kanban_db.get_task(conn, task_id).status == "review"
    with pytest.raises(
        kanban_db.CoordinationLaunchDeferred, match="recovery verification"
    ):
        kanban_db.claim_task_for_dispatch(
            conn, task_id, review=True, organization=ORG, now=now + 86_400,
        )

    with kanban_db.write_txn(conn):
        kanban_db._append_event(
            conn,
            task_id,
            "workforce_handoff_recovery_verified",
            {
                "failure_event_id": "failure-1",
                "failure_order": 10,
                "success_event_ids": ["success-1", "success-2"],
                "success_orders": [11, 12],
                "required_successes": 2,
            },
        )
    reviewer, _ = kanban_db.claim_task_for_dispatch(
        conn, task_id, review=True, organization=ORG, now=now + 86_401,
    )
    assert reviewer is not None
    assert reviewer.assignee == "aurora"


def test_source_acceptance_requires_target_ack_and_source_review_run(conn):
    now = int(time.time())
    created = create_handoff(
        conn,
        source_agent="emily",
        target_agent="sage",
        expected_outcome="Repair the scheduled product workflow",
        acceptance_test="Two distinct scheduled executions succeed",
        evidence_references=["cron:job-1"],
        acknowledgment_deadline=_iso(now + 60),
        checkpoint_at=_iso(now + 240),
        organization=ORG,
        requires_source_acceptance=True,
    )
    task_id = created["task_id"]

    # A target cannot close its own work without returning it to the source.
    acknowledge_handoff(
        conn, task_id, actor="sage", organization=ORG, now=now + 1
    )
    owner_run = kanban_db.claim_task(conn, task_id, claimer="sage:test")
    assert owner_run is not None
    assert kanban_db.complete_task(
        conn,
        task_id,
        summary="Implementation finished without review",
        expected_run_id=owner_run.current_run_id,
    ) is False

    ok, reason = kanban_db.request_review(
        conn,
        task_id,
        summary="Repair evidence attached",
        reviewer="xenia",
        expected_run_id=owner_run.current_run_id,
        with_reason=True,
    )
    assert ok is False
    assert reason == "handoff review must return to its source"

    assert kanban_db.request_review(
        conn,
        task_id,
        summary="Repair evidence attached",
        expected_run_id=owner_run.current_run_id,
    )
    assert kanban_db.get_task(conn, task_id).assignee == "emily"
    review_run = kanban_db.claim_review_task(
        conn, task_id, claimer="emily:test"
    )
    assert review_run is not None
    assert kanban_db.complete_task(
        conn,
        task_id,
        summary="Accepted after source-owned verification",
        expected_run_id=review_run.current_run_id,
    )
