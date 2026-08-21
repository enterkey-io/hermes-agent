from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli import workflow_registry as reg
from hermes_cli.workflow_models import WorkflowConflictError, WorkflowStateError


@pytest.fixture
def conn(tmp_path: Path):
    with reg.connect_closing(tmp_path / "workflow_registry.db") as db:
        yield db


def _definition(conn, slug: str = "morning-message"):
    return reg.create_definition(
        conn,
        id=f"wf-{slug}",
        slug=slug,
        name="Morning Message",
        owner_profile="grace",
        status="active",
        runtime_kind="script",
        runtime_ref="/tmp/morning.py",
        source_path="/runbooks/morning/RUNBOOK.md",
        source_hash="sha256:abc",
        source_revision="1",
        dedupe_strategy="daily",
        timeout_seconds=600,
        max_attempts=2,
    )


def test_schema_idempotent_and_definition_crud(conn) -> None:
    reg.init_db(conn)
    workflow = _definition(conn)

    fetched = reg.get_definition(conn, workflow.id)
    assert fetched.slug == "morning-message"
    assert fetched.version == 1
    assert reg.get_definition_by_slug(conn, "morning-message").id == workflow.id

    updated = reg.update_definition(
        conn,
        workflow.id,
        expected_version=fetched.version,
        name="Morning Brief",
        source_revision="2",
    )
    assert updated.name == "Morning Brief"
    assert updated.version == 2

    with pytest.raises(WorkflowConflictError):
        reg.update_definition(
            conn,
            workflow.id,
            expected_version=fetched.version,
            name="Stale Write",
        )


def test_schema_migrates_existing_definition_table_with_workforce_marker(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE workflow_definitions (
                id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                description TEXT, owner_profile TEXT NOT NULL, status TEXT NOT NULL,
                runtime_kind TEXT NOT NULL, runtime_ref TEXT, source_path TEXT,
                source_hash TEXT, source_revision TEXT, kanban_board TEXT,
                repair_task_id TEXT, dedupe_strategy TEXT, timeout_seconds INTEGER,
                max_attempts INTEGER, version INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                retired_at INTEGER
            )
            """
        )
    with reg.connect_closing(db_path) as migrated:
        columns = {
            row["name"]
            for row in migrated.execute("PRAGMA table_info(workflow_definitions)")
        }
        assert "workforce_managed" in columns
        assert migrated.execute(
            "SELECT value FROM workflow_registry_meta WHERE key = 'schema_version'"
        ).fetchone()["value"] == str(reg.SCHEMA_VERSION)


def test_replace_steps_and_contract_round_trip(conn) -> None:
    workflow = _definition(conn)
    steps = reg.replace_steps(
        conn,
        workflow.id,
        [
            {
                "step_key": "collect",
                "position": 0,
                "name": "Collect inputs",
                "input_contract": {"calendar": "read"},
                "output_contract": {"brief": "markdown"},
            },
            {
                "step_key": "deliver",
                "position": 1,
                "name": "Deliver",
                "executor_profile": "grace",
                "runtime_kind": "script",
            },
        ],
    )

    assert [step.step_key for step in steps] == ["collect", "deliver"]
    assert steps[0].input_contract == {"calendar": "read"}
    assert steps[0].output_contract == {"brief": "markdown"}

    with pytest.raises(WorkflowConflictError):
        reg.replace_steps(
            conn,
            workflow.id,
            [
                {"step_key": "same", "position": 0, "name": "A"},
                {"step_key": "same", "position": 1, "name": "B"},
            ],
        )


def test_deduplicated_run_reuses_existing(conn) -> None:
    workflow = _definition(conn)
    first = reg.start_run(
        conn,
        workflow.id,
        trigger_kind="cron",
        trigger_ref="grace/job-1",
        dedupe_key="2026-08-07",
    )
    second = reg.start_run(
        conn,
        workflow.id,
        trigger_kind="cron",
        trigger_ref="grace/job-1",
        dedupe_key="2026-08-07",
    )

    assert second.id == first.id
    assert len(reg.list_runs(conn, workflow.id)) == 1

    with pytest.raises(WorkflowConflictError):
        reg.start_run(
            conn,
            workflow.id,
            trigger_kind="cron",
            dedupe_key="2026-08-07",
            reuse_existing=False,
        )


def test_step_and_run_transitions(conn) -> None:
    workflow = _definition(conn)
    run = reg.start_run(conn, workflow.id, trigger_kind="manual")
    step = reg.start_step(conn, run.id, "collect")

    completed = reg.finish_step(
        conn,
        step.id,
        status="succeeded",
        summary="Collected.",
        output_refs={"file": "/tmp/brief.md"},
    )
    assert completed.status == "succeeded"
    assert completed.output_refs == {"file": "/tmp/brief.md"}
    assert reg.get_run(conn, run.id).current_step_key == "collect"

    final = reg.complete_run(conn, run.id, summary="Done.")
    assert final.status == "succeeded"
    assert final.ended_at is not None

    with pytest.raises(WorkflowStateError):
        reg.start_step(conn, run.id, "deliver")
    with pytest.raises(WorkflowStateError):
        reg.finish_step(conn, step.id, status="failed", error="late")


def test_concurrent_dedupe_allows_one_run(tmp_path: Path) -> None:
    db_path = tmp_path / "workflow_registry.db"
    with reg.connect_closing(db_path) as setup:
        workflow = _definition(setup)

    run_ids: list[str] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            with reg.connect_closing(db_path) as conn:
                run = reg.start_run(
                    conn,
                    workflow.id,
                    trigger_kind="cron",
                    dedupe_key="same-window",
                )
                run_ids.append(run.id)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(set(run_ids)) == 1
    with reg.connect_closing(db_path) as conn:
        assert len(reg.list_runs(conn, workflow.id)) == 1


def test_backup_export_and_restore(conn, tmp_path: Path) -> None:
    workflow = _definition(conn)
    reg.start_run(conn, workflow.id, trigger_kind="manual", dedupe_key="backup")

    exported = reg.export_json(conn)
    assert exported["schema_version"] == reg.SCHEMA_VERSION
    assert len(exported["tables"]["workflow_definitions"]) == 1
    json.dumps(exported)

    backup = reg.backup_db(conn, tmp_path / "backup.db")
    restored = reg.restore_backup(backup, tmp_path / "restored.db")
    with reg.connect_closing(restored) as restored_conn:
        assert reg.get_definition(restored_conn, workflow.id).slug == workflow.slug
        assert len(reg.list_runs(restored_conn, workflow.id)) == 1


def test_profile_references_do_not_load_profile_credentials(conn) -> None:
    workflow = reg.create_definition(
        conn,
        slug="profile-isolated",
        name="Profile Isolated",
        owner_profile="grace",
        status="active",
        runtime_kind="hermes",
    )
    reg.link_schedule(
        conn,
        workflow.id,
        profile="grace",
        cron_job_id="job-1",
        enabled=True,
        last_verified_at=123,
    )
    exported = reg.export_json(conn)
    rendered = json.dumps(exported)

    assert "grace" in rendered
    assert ".env" not in rendered
    assert "token" not in rendered.lower()


def test_prune_missing_schedule_links_keeps_only_live_jobs(conn) -> None:
    workflow = _definition(conn)
    reg.link_schedule(conn, workflow.id, profile="grace", cron_job_id="live")
    reg.link_schedule(conn, workflow.id, profile="grace", cron_job_id="stale")

    pruned = reg.prune_missing_schedule_links(conn, {("grace", "live")})

    assert pruned == [
        {"workflow_id": workflow.id, "profile": "grace", "cron_job_id": "stale"}
    ]
    rows = conn.execute(
        "SELECT profile, cron_job_id FROM workflow_schedules"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("grace", "live")]


def test_reconcile_schedule_links_adds_updates_and_removes_drift(conn) -> None:
    one = _definition(conn, "one")
    two = _definition(conn, "two")
    reg.link_schedule(conn, one.id, profile="grace", cron_job_id="disabled", enabled=True)
    reg.link_schedule(conn, one.id, profile="grace", cron_job_id="removed", enabled=True)

    result = reg.reconcile_schedule_links(conn, [
        {"profile": "grace", "cron_job_id": "disabled", "workflow_id": one.id, "enabled": False},
        {"profile": "main", "cron_job_id": "new", "workflow_id": two.id, "enabled": True},
    ])

    assert [(row["cron_job_id"], row["enabled"]) for row in result["updated"]] == [("disabled", False)]
    assert [row["cron_job_id"] for row in result["removed"]] == ["removed"]
    assert [row["cron_job_id"] for row in result["added"]] == ["new"]
    rows = conn.execute(
        "SELECT profile,cron_job_id,enabled FROM workflow_schedules ORDER BY profile,cron_job_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("grace", "disabled", 0), ("main", "new", 1)]


def test_settle_orphaned_runs_closes_waiting_and_stale_parents(conn) -> None:
    workflow = _definition(conn)
    waiting = reg.start_run(conn, workflow.id, trigger_kind="cron")
    waiting_step = reg.start_step(conn, waiting.id, "collect")
    reg.finish_step(conn, waiting_step.id, status="waiting_for_approval")
    stale = reg.start_run(conn, workflow.id, trigger_kind="cron")
    stale_step = reg.start_step(conn, stale.id, "collect")
    with reg.write_txn(conn):
        conn.execute("UPDATE workflow_runs SET started_at=1 WHERE id=?", (stale.id,))

    settled = reg.settle_orphaned_runs(conn, max_age_seconds=100, now=1000)

    assert {row["run_id"] for row in settled} == {waiting.id, stale.id}
    assert reg.get_run(conn, waiting.id).status == "cancelled"
    assert reg.get_run(conn, stale.id).status == "cancelled"
    assert reg.get_step_run(conn, stale_step.id).status == "cancelled"


def test_settle_orphaned_runs_closes_parent_superseded_by_terminal_successor(conn) -> None:
    workflow = _definition(conn)
    orphan = reg.start_run(conn, workflow.id, trigger_kind="cron", trigger_ref="job")
    successor = reg.start_run(conn, workflow.id, trigger_kind="cron", trigger_ref="job")
    with reg.write_txn(conn):
        conn.execute("UPDATE workflow_runs SET started_at=1 WHERE id=?", (orphan.id,))
        conn.execute("UPDATE workflow_runs SET started_at=2 WHERE id=?", (successor.id,))
    reg.complete_run(conn, successor.id)

    settled = reg.settle_orphaned_runs(conn, max_age_seconds=9999, now=100)

    assert settled == [{
        "run_id": orphan.id,
        "reason": f"superseded by terminal successor {successor.id}",
    }]
    assert reg.get_run(conn, orphan.id).status == "cancelled"


def test_canonical_organization_rejects_friend_owner(conn, monkeypatch) -> None:
    org_path = Path(__file__).parents[2] / "workforce" / "organization.yaml"
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(org_path))
    with pytest.raises(ValueError, match="cannot own or execute"):
        reg.create_definition(
            conn, slug="friend-owned", name="Friend Owned",
            owner_profile="amy", workforce_managed=True,
            status="active", runtime_kind="hermes",
        )


def test_canonical_organization_rejects_friend_executor(conn, monkeypatch) -> None:
    org_path = Path(__file__).parents[2] / "workforce" / "organization.yaml"
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(org_path))
    workflow = reg.create_definition(
        conn, slug="org-aware", name="Org aware", owner_profile="grace",
        workforce_managed=True, status="active", runtime_kind="hermes",
    )
    with pytest.raises(ValueError, match="cannot own or execute"):
        reg.replace_steps(
            conn, workflow.id,
            [{"step_key": "bad", "name": "Bad", "executor_profile": "kourtnie"}],
        )


def test_generic_workflow_is_not_constrained_by_workforce_roster(conn, monkeypatch) -> None:
    org_path = Path(__file__).parents[2] / "workforce" / "organization.yaml"
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(org_path))
    workflow = reg.create_definition(
        conn, slug="friend-personal", name="Friend personal workflow",
        owner_profile="amy", status="active", runtime_kind="hermes",
    )
    reg.replace_steps(
        conn, workflow.id,
        [{"step_key": "chat", "name": "Chat", "executor_profile": "kourtnie"}],
    )
    reg.link_schedule(conn, workflow.id, profile="amy", cron_job_id="personal")
    assert reg.start_run(conn, workflow.id, trigger_kind="manual").status == "running"
