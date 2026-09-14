"""Outcome-aware Kanban follow-through and origin reporting regressions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban_db as kb
from tests.workforce_test_helpers import materialize_test_organization


ROOT = Path(__file__).parents[2]


@pytest.fixture
def followthrough_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    org_path = materialize_test_organization(
        ROOT / "workforce" / "organization.yaml", tmp_path
    )
    organization = yaml.safe_load(org_path.read_text(encoding="utf-8"))
    for item in organization["agents"]:
        if not item.get("operational") or item.get("status") != "active":
            continue
        profile = Path(item["profile_path"])
        profile.joinpath("config.yaml").write_text(
            "model:\n  default: test-model\n  provider: test-provider\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(org_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _events(conn: sqlite3.Connection, task_id: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"] or "{}") for row in rows]


def _accept_telegram_request(
    conn: sqlite3.Connection,
) -> tuple[str, kb.CoordinationRequest]:
    root_id = kb.create_task(
        conn,
        title="Return the installed and verified repair",
        assignee="aurora",
        session_id="agent:xenia:telegram:dm:1225948997",
    )
    kb.add_notify_sub(
        conn,
        task_id=root_id,
        platform="telegram",
        chat_id="1225948997",
        notifier_profile="xenia",
        delivery_mode="wake",
        chat_type="dm",
    )
    request = kb.create_coordination_request(
        conn,
        root_task_id=root_id,
        origin_session_id="agent:xenia:telegram:dm:1225948997",
        origin_message_id="telegram-message-42",
        now=100,
    )
    return root_id, request


def _managed_repair(conn: sqlite3.Connection, root_id: str) -> str:
    return kb.create_task(
        conn,
        title="Repair, independently verify, activate, and accept delivery identity",
        body="The installed Telegram path must deliver exactly one final response.",
        assignee="sloane",
        created_by="aurora",
        workspace_kind="scratch",
        coordination_source_task_id=root_id,
        lifecycle_type="software",
        original_author="aurora",
        implementer="sloane",
        technical_reviewer="reese",
        intent_validator="aurora",
        activation_owner="alina",
        closure_owner="aurora",
        current_phase="execution",
        return_to="sloane",
        idempotency_key="telegram-delivery-lifecycle-v1",
    )


def test_failed_review_requeues_implementer_reports_once_then_fresh_pass_closes(
    followthrough_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Xenia incident lifecycle cannot strand work after failed QA."""
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        root_id, request = _accept_telegram_request(conn)
        repair_id = _managed_repair(conn, root_id)

        implementation = kb.claim_task(conn, repair_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            repair_id,
            summary="Candidate cb4a25b implemented with focused tests.",
            metadata={"candidate_revision": "cb4a25b", "tests": ["focused"]},
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, repair_id)
        assert review is not None
        assert kb.request_changes(
            conn,
            repair_id,
            reason=(
                "P1: rejected transformed edits are recorded as delivered; gate the "
                "receipt on SendResult.success and add a failure-path regression."
            ),
            expected_run_id=review.current_run_id,
        ) == (True, "sloane")

        repair = kb.get_task(conn, repair_id)
        assert repair is not None
        assert (repair.status, repair.assignee, repair.current_phase) == (
            "ready",
            "sloane",
            "execution",
        )
        checkpoints = _events(conn, root_id, "coordination_checkpoint")
        assert len(checkpoints) == 1
        assert checkpoints[0]["checkpoint_kind"] == "qa_failed_rework"
        assert checkpoints[0]["source_task_id"] == repair_id
        assert checkpoints[0]["next_owner"] == "sloane"
        assert checkpoints[0]["status"] == "remediation_underway"

        graph = kb.task_graph_status(conn, root_id)
        assert graph["overall_state"] == "active"
        assert graph["active"][0]["id"] == repair_id
        assert graph["active"][0]["assignee"] == "sloane"
        assert graph["failed_reviews"][0]["task_id"] == repair_id
        assert graph["next_owner"] == "sloane"
        assert graph["automatic_final_report"] == {
            "configured": True,
            "request_root_id": request.id,
            "status": "active",
            "responsible_agent": "aurora",
        }

        corrected = kb.claim_task(conn, repair_id)
        assert corrected is not None
        assert kb.request_review(
            conn,
            repair_id,
            summary="Corrected revision f28ef9b with exact failure coverage.",
            metadata={"candidate_revision": "f28ef9b", "tests": ["failure path"]},
            expected_run_id=corrected.current_run_id,
        )
        fresh_review = kb.claim_review_task(conn, repair_id)
        assert fresh_review is not None
        assert kb.pass_review(
            conn,
            repair_id,
            summary="PASS: exact revision independently reproduced.",
            metadata={"verdict": "pass", "candidate_revision": "f28ef9b"},
            expected_run_id=fresh_review.current_run_id,
        ) == (True, "aurora")
        checkpoints = _events(conn, root_id, "coordination_checkpoint")
        assert [item["checkpoint_kind"] for item in checkpoints] == [
            "qa_failed_rework",
            "qa_passed",
        ]

        intent = kb.claim_task(conn, repair_id)
        assert intent is not None
        assert kb.handoff_task(
            conn,
            repair_id,
            next_assignee="alina",
            next_phase="activation",
            summary="Intent matches the requested exactly-once behavior.",
            evidence={"reviewed_revision": "f28ef9b"},
            expected_outcome="Deploy only the reviewed revision.",
            recheck_condition="Installed fake-sink acceptance passes.",
            expected_run_id=intent.current_run_id,
        ) == (True, "alina")
        activation = kb.claim_task(conn, repair_id)
        assert activation is not None
        assert kb.handoff_task(
            conn,
            repair_id,
            next_assignee="aurora",
            next_phase="live_acceptance",
            summary="Reviewed revision deployed and service canary passed.",
            evidence={"revision": "f28ef9b", "canary": "pass"},
            expected_outcome="Verify the installed workflow.",
            recheck_condition="One persistent final is observed before maintenance.",
            expected_run_id=activation.current_run_id,
        ) == (True, "aurora")
        acceptance = kb.claim_task(conn, repair_id)
        assert acceptance is not None
        assert kb.complete_task(
            conn,
            repair_id,
            summary="Installed fake-sink workflow accepted.",
            metadata={"live_evidence": ["exactly one persistent final"]},
            expected_run_id=acceptance.current_run_id,
        )

        resolved_graph = kb.task_graph_status(conn, root_id)
        assert resolved_graph["overall_state"] == "completed"
        assert resolved_graph["failed_reviews"] == []

        assert kb.begin_coordination_final_return_if_ready(conn, request.id, now=200)
        pending = _events(conn, root_id, "coordination_return_pending")
        assert len(pending) == 1
        assert kb.complete_task(conn, root_id, summary="Verified repair returned once.")
        delivery = kb.prepare_coordination_final_return_deliveries(
            conn, notifier_profiles={"xenia"}, notifier_agents={"aurora"}, now=201
        )
        assert len(delivery) == 1
        assert kb.acknowledge_coordination_return(
            conn,
            request.id,
            event_id=delivery[0]["event_id"],
            responsible_agent="aurora",
            returned_message_id="telegram:final:1",
            now=202,
        )
        assert not kb.acknowledge_coordination_return(
            conn,
            request.id,
            event_id=delivery[0]["event_id"],
            responsible_agent="aurora",
            returned_message_id="telegram:final:1",
            now=203,
        )
        assert kb.prepare_coordination_final_return_deliveries(
            conn, notifier_profiles={"xenia"}, notifier_agents={"aurora"}, now=204
        ) == []


def test_success_gated_dependency_rejects_failed_qa_but_completion_edge_accepts_it(
    followthrough_env: Path,
) -> None:
    with kb.connect() as conn:
        implementation_id = kb.create_task(
            conn, title="Implementation", assignee="sloane"
        )
        assert kb.complete_task(
            conn,
            implementation_id,
            metadata={"candidate_revision": "bad-revision"},
        )
        qa_id = kb.create_task(
            conn, title="Independent QA", assignee="reese", parents=[implementation_id]
        )
        release_id = kb.create_task(
            conn, title="Release", assignee="root", parents=[qa_id]
        )
        assert kb.complete_task(
            conn,
            qa_id,
            summary="QA found a reproducible P1 defect.",
            metadata={"verdict": "fail", "candidate_revision": "bad-revision"},
        )

        release = kb.get_task(conn, release_id)
        assert release is not None and release.status == "todo"
        assert not kb._parents_satisfied(conn, release_id)
        assert kb.recompute_ready(conn) == 0
        assert kb.claim_task(conn, release_id) is None
        promoted, reason = kb.promote_task(
            conn, release_id, actor="operator", reason="attempt unsafe release"
        )
        assert not promoted
        assert reason is not None and qa_id in reason
        link = conn.execute(
            "SELECT required_outcome FROM task_links "
            "WHERE parent_id = ? AND child_id = ?",
            (qa_id, release_id),
        ).fetchone()
        assert link["required_outcome"] == "success"

        report_id = kb.create_task(
            conn,
            title="Report the failed gate",
            assignee="aurora",
            parents=[qa_id],
            parent_outcome="completion",
        )
        report = kb.get_task(conn, report_id)
        assert report is not None and report.status == "ready"
        graph = kb.task_graph_status(conn, release_id)
        assert graph["overall_state"] == "failed"
        assert graph["failed_gates"] == [
            {
                "parent_id": qa_id,
                "parent_title": "Independent QA",
                "child_id": release_id,
                "child_title": "Release",
                "required_outcome": "success",
                "observed_outcome": "failure",
                "verdict": "fail",
            }
        ]
        assert graph["next_owner"] == "sloane"
        assert "remediat" in graph["next_action"].lower()


@pytest.mark.parametrize("source_status", ["ready", "running", "blocked", "review"])
def test_archiving_unfinished_parent_releases_only_completion_edge(
    followthrough_env: Path, source_status: str
) -> None:
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title=f"Cancelled {source_status} parent")
        if source_status == "running":
            assert kb.claim_task(conn, parent_id) is not None
        elif source_status == "blocked":
            assert kb.block_task(conn, parent_id, reason="Cancelled dependency")
        elif source_status == "review":
            implementation = kb.claim_task(conn, parent_id)
            assert implementation is not None
            assert kb.request_review(
                conn,
                parent_id,
                reviewer="reese",
                summary="Candidate awaiting review.",
                metadata={"candidate_revision": "cancelled"},
                expected_run_id=implementation.current_run_id,
            )

        success_child = kb.create_task(
            conn, title="Unsafe release", parents=[parent_id]
        )
        completion_child = kb.create_task(
            conn,
            title="Cancellation report",
            parents=[parent_id],
            parent_outcome="completion",
        )
        assert kb.get_task(conn, success_child).status == "todo"
        assert kb.get_task(conn, completion_child).status == "todo"

        assert kb.archive_task(conn, parent_id)

        assert kb.task_terminal_outcome(conn, parent_id) == {
            "outcome": "failure",
            "verdict": None,
            "terminal": True,
        }
        assert kb.get_task(conn, success_child).status == "todo"
        assert kb.claim_task(conn, success_child) is None
        assert kb.get_task(conn, completion_child).status == "ready"


def test_archiving_done_parent_preserves_frozen_success_or_failure(
    followthrough_env: Path,
) -> None:
    with kb.connect() as conn:
        successful = kb.create_task(conn, title="Successful prerequisite")
        failed = kb.create_task(conn, title="Failed prerequisite")
        assert kb.complete_task(conn, successful, metadata={"verdict": "pass"})
        assert kb.complete_task(conn, failed, metadata={"verdict": "fail"})

        assert kb.archive_task(conn, successful)
        assert kb.archive_task(conn, failed)

        assert kb.task_terminal_outcome(conn, successful) == {
            "outcome": "success",
            "verdict": "pass",
            "terminal": True,
        }
        assert kb.task_terminal_outcome(conn, failed) == {
            "outcome": "failure",
            "verdict": "fail",
            "terminal": True,
        }


def test_archiving_unfinished_coordination_work_enters_guardrail(
    followthrough_env: Path,
) -> None:
    with kb.connect() as conn:
        root_id, request = _accept_telegram_request(conn)
        repair_id = _managed_repair(conn, root_id)

        assert kb.archive_task(conn, repair_id)

        refreshed = kb.get_coordination_request(conn, request.id)
        assert refreshed is not None and refreshed.status == "return_pending"
        assert _events(conn, repair_id, "terminal_outcome_failed") == [
            {
                "request_root_id": request.id,
                "required_outcome": None,
                "observed_outcome": "failure",
                "verdict": None,
                "blocked_children": [],
            }
        ]
        assert len(_events(conn, root_id, "coordination_guardrail_reached")) == 1


def test_terminal_outcome_is_immutable_after_completed_metadata_edit(
    followthrough_env: Path,
) -> None:
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="Failed QA", assignee="reese")
        child_id = kb.create_task(
            conn, title="Release", assignee="root", parents=[parent_id]
        )
        assert kb.complete_task(
            conn,
            parent_id,
            summary="QA failed.",
            metadata={"verdict": "fail"},
        )
        frozen = kb.get_task(conn, parent_id)
        assert frozen is not None
        assert (frozen.terminal_outcome, frozen.terminal_verdict) == (
            "failure",
            "fail",
        )

        assert kb.edit_completed_task_result(
            conn,
            parent_id,
            result="Operator added a later note.",
            metadata={"verdict": "pass"},
        )
        assert kb.task_terminal_outcome(conn, parent_id) == {
            "outcome": "failure",
            "verdict": "fail",
            "terminal": True,
        }
        child = kb.get_task(conn, child_id)
        assert child is not None and child.status == "todo"
        assert not kb._parents_satisfied(conn, child_id)


def test_failed_review_cannot_be_encoded_as_successful_completion(
    followthrough_env: Path,
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Legacy review", assignee="sloane")
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            reviewer="reese",
            summary="Candidate ready for review.",
            metadata={"candidate_revision": "bad-revision"},
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id)
        assert review is not None
        with pytest.raises(kb.ReviewOutcomeError, match="request_changes"):
            kb.complete_task(
                conn,
                task_id,
                summary="QA failed.",
                metadata={"verdict": "fail"},
                expected_run_id=review.current_run_id,
            )
        running_review = kb.get_task(conn, task_id)
        assert running_review is not None and running_review.status == "running"
        assert kb.request_changes(
            conn,
            task_id,
            reason="The candidate drops the final response after a rejected edit.",
            expected_run_id=review.current_run_id,
        ) == (True, "sloane")


def test_parked_failed_review_cannot_be_completed_without_request_changes(
    followthrough_env: Path,
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Parked review", assignee="sloane")
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            reviewer="reese",
            summary="Candidate ready for review.",
            metadata={"candidate_revision": "bad-revision"},
            expected_run_id=implementation.current_run_id,
        )

        with pytest.raises(kb.ReviewOutcomeError, match="request_changes"):
            kb.complete_task(
                conn,
                task_id,
                summary="QA failed before the review card was claimed.",
                metadata={"verdict": "fail"},
            )

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert (task.status, task.assignee) == ("review", "reese")


def test_legacy_link_migration_preserves_completion_semantics_for_existing_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "kanban.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
            status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
            completed_at INTEGER, workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT, claim_lock TEXT, claim_expires INTEGER
        );
        CREATE TABLE task_links (
            parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
            PRIMARY KEY (parent_id, child_id)
        );
        INSERT INTO tasks (id,title,status,created_at,workspace_kind)
            VALUES ('old-parent','old parent','done',1,'scratch');
        INSERT INTO tasks (id,title,status,created_at,workspace_kind)
            VALUES ('old-child','old child','todo',2,'scratch');
        INSERT INTO task_links VALUES ('old-parent','old-child');
        """
    )
    legacy.commit()
    legacy.close()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(db_path)

    with kb.connect(db_path) as conn:
        old = conn.execute(
            "SELECT required_outcome FROM task_links "
            "WHERE parent_id='old-parent' AND child_id='old-child'"
        ).fetchone()
        assert old["required_outcome"] == "completion"
        new_parent = kb.create_task(conn, title="new parent", assignee="sloane")
        new_child = kb.create_task(conn, title="new child", assignee="reese")
        kb.link_tasks(conn, new_parent, new_child)
        new = conn.execute(
            "SELECT required_outcome FROM task_links WHERE parent_id=? AND child_id=?",
            (new_parent, new_child),
        ).fetchone()
        assert new["required_outcome"] == "success"


def test_legacy_done_outcome_backfill_freezes_latest_completion_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "kanban.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
            status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
            completed_at INTEGER, workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT, claim_lock TEXT, claim_expires INTEGER
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            profile TEXT, step_key TEXT, status TEXT NOT NULL, claim_lock TEXT,
            claim_expires INTEGER, worker_pid INTEGER, max_runtime_seconds INTEGER,
            last_heartbeat_at INTEGER, started_at INTEGER NOT NULL, ended_at INTEGER,
            outcome TEXT, summary TEXT, metadata TEXT, error TEXT
        );
        INSERT INTO tasks (id,title,status,created_at,completed_at,workspace_kind)
            VALUES ('legacy-fail','old QA','done',1,2,'scratch');
        INSERT INTO task_runs (
            task_id,status,started_at,ended_at,outcome,metadata
        ) VALUES (
            'legacy-fail','done',1,2,'completed','{"verdict":"fail"}'
        );
        """
    )
    legacy.commit()
    legacy.close()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(db_path)

    with kb.connect(db_path) as conn:
        task = kb.get_task(conn, "legacy-fail")
        assert task is not None
        assert (task.terminal_outcome, task.terminal_verdict) == (
            "failure",
            "fail",
        )
        assert kb.task_terminal_outcome(conn, task.id) == {
            "outcome": "failure",
            "verdict": "fail",
            "terminal": True,
        }


def test_blocked_coordination_child_stalls_graph_and_returns_once(
    followthrough_env: Path,
) -> None:
    with kb.connect() as conn:
        root_id, request = _accept_telegram_request(conn)
        child_id = kb.create_task(
            conn,
            title="Release reviewed revision",
            assignee="root",
            coordination_source_task_id=root_id,
        )
        child = kb.claim_task(conn, child_id)
        assert child is not None
        assert kb.block_task(
            conn,
            child_id,
            reason="Reviewed revision is absent from the canonical branch.",
            kind="capability",
            expected_run_id=child.current_run_id,
        )

        current_request = kb.get_coordination_request(conn, request.id)
        root = kb.get_task(conn, root_id)
        assert current_request is not None and current_request.status == "return_pending"
        assert root is not None and root.status == "blocked"
        assert len(_events(conn, root_id, "coordination_guardrail_reached")) == 1
        graph = kb.task_graph_status(conn, root_id)
        assert graph["overall_state"] == "stalled"
        assert graph["blocked"][0]["id"] == child_id
        assert graph["next_owner"] == "root"
        assert graph["automatic_final_report"]["status"] == "return_pending"
        deliveries = kb.prepare_coordination_final_return_deliveries(
            conn, notifier_profiles={"xenia"}, notifier_agents={"aurora"}, now=101
        )
        assert len(deliveries) == 1
        assert deliveries[0]["event_kind"] == "coordination_guardrail_reached"


def test_coordination_dependency_wait_stays_active_for_parent_recompute(
    followthrough_env: Path,
) -> None:
    with kb.connect() as conn:
        root_id, request = _accept_telegram_request(conn)
        parent_id = kb.create_task(
            conn,
            title="Prepare reviewed revision",
            assignee="sloane",
            coordination_source_task_id=root_id,
        )
        child_id = kb.create_task(
            conn,
            title="Release reviewed revision",
            assignee="root",
            coordination_source_task_id=root_id,
        )
        child = kb.claim_task(conn, child_id)
        assert child is not None
        kb.link_tasks(conn, parent_id, child_id)
        assert kb.block_task(
            conn,
            child_id,
            reason="Waiting for the linked preparation task.",
            kind="dependency",
            expected_run_id=child.current_run_id,
        )

        current_request = kb.get_coordination_request(conn, request.id)
        root = kb.get_task(conn, root_id)
        child = kb.get_task(conn, child_id)
        assert current_request is not None and current_request.status == "active"
        assert root is not None and root.status == "ready"
        assert child is not None and child.status == "todo"
        assert _events(conn, root_id, "coordination_guardrail_reached") == []
        graph = kb.task_graph_status(conn, root_id)
        assert graph["overall_state"] == "active"
        assert graph["next_owner"] == "sloane"


def test_failed_success_gate_reports_failed_even_when_guardrail_blocks_root(
    followthrough_env: Path,
) -> None:
    with kb.connect() as conn:
        root_id, request = _accept_telegram_request(conn)
        implementation_id = kb.create_task(
            conn,
            title="Implementation",
            assignee="sloane",
            coordination_source_task_id=root_id,
        )
        assert kb.complete_task(conn, implementation_id)
        qa_id = kb.create_task(
            conn,
            title="Independent QA",
            assignee="reese",
            parents=[implementation_id],
            coordination_source_task_id=root_id,
        )
        release_id = kb.create_task(
            conn,
            title="Release",
            assignee="root",
            parents=[qa_id],
            coordination_source_task_id=root_id,
        )

        assert kb.complete_task(
            conn,
            qa_id,
            summary="QA found a reproducible failure.",
            metadata={"verdict": "fail"},
        )

        current_request = kb.get_coordination_request(conn, request.id)
        root = kb.get_task(conn, root_id)
        release = kb.get_task(conn, release_id)
        assert current_request is not None and current_request.status == "return_pending"
        assert root is not None and root.status == "blocked"
        assert release is not None and release.status == "todo"
        graph = kb.task_graph_status(conn, root_id)
        assert graph["overall_state"] == "failed"
        assert graph["failed_gates"][0]["parent_id"] == qa_id
        assert graph["next_owner"] == "sloane"
        assert graph["automatic_final_report"]["status"] == "return_pending"


def test_failed_coordination_leaf_triggers_guardrail_and_failed_graph(
    followthrough_env: Path,
) -> None:
    with kb.connect() as conn:
        root_id, request = _accept_telegram_request(conn)
        leaf_id = kb.create_task(
            conn,
            title="Independent acceptance",
            assignee="reese",
            coordination_source_task_id=root_id,
        )

        assert kb.complete_task(
            conn,
            leaf_id,
            summary="Acceptance found a release-blocking defect.",
            metadata={"verdict": "fail"},
        )

        current_request = kb.get_coordination_request(conn, request.id)
        root = kb.get_task(conn, root_id)
        assert current_request is not None
        assert current_request.status == "return_pending"
        assert root is not None and root.status == "blocked"
        assert not kb.begin_coordination_final_return_if_ready(conn, request.id)
        graph = kb.task_graph_status(conn, root_id)
        assert graph["overall_state"] == "failed"
        assert graph["failed_outcomes"] == [
            {
                "task_id": leaf_id,
                "task_title": "Independent acceptance",
                "observed_outcome": "failure",
                "verdict": "fail",
            }
        ]
        assert graph["next_owner"] == "reese"


def test_show_surface_includes_whole_graph_status(followthrough_env: Path) -> None:
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        parent = kb.create_task(conn, title="Implementation", assignee="sloane")
        child = kb.create_task(conn, title="QA", assignee="reese", parents=[parent])

    payload = json.loads(kt._handle_show({"task_id": child}))
    assert payload["graph_status"]["overall_state"] == "active"
    assert {item["id"] for item in payload["graph_status"]["tasks"]} == {
        parent,
        child,
    }
    assert payload["graph_status"]["automatic_final_report"] == {
        "configured": False,
        "request_root_id": None,
        "status": "not_configured",
        "responsible_agent": None,
    }
