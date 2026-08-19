from __future__ import annotations

from pathlib import Path
import time

import pytest

from hermes_cli import kanban_db
from hermes_cli.workforce_org import load_organization
from plugins.workforce_control.store import (
    apply_reconciliation,
    dashboard_snapshot,
    materialize_plan,
    observe_dispatch_tick,
    propose_reconciliation,
    record_correction,
    record_plan,
    record_signal,
    runtime_state,
    set_runtime_mode,
)


ROOT = Path(__file__).parents[2]


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    conn = kanban_db.connect(tmp_path / "kanban.db")
    runtime_state(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="module")
def organization():
    return load_organization(ROOT / "workforce/organization.yaml")


def plan_payload(*, nodes=None, unresolved=None):
    return {
        "title": "Ship the controlled workforce observer",
        "goal_ref": "evernote:goal/proactive-workforce",
        "goal_evidence_at": int(time.time()),
        "desired_outcome": "The workforce finds and closes useful work without speculative fan-out",
        "acceptance_test": "Named proactive scenarios pass with no unauthorized external action",
        "priority_rationale": "This is Elliott's active operating-system priority",
        "checkpoint": "After the first isolated whole-workforce simulation",
        "capacity_assessment": "One bounded implementation node; no competing production work",
        "deadline_dependencies": "No external deadline; depends on isolated test state",
        "displaced_work": "None; implementation remains isolated",
        "unresolved_decisions": list(unresolved or []),
        "defer_or_stop": "Stop if current-state evidence is stale or acceptance fails",
        "evidence_references": ["file://authoritative-plan"],
        "nodes": nodes or [
            {
                "key": "implementation",
                "title": "Implement the bounded observer",
                "assignee": "sloane",
                "responsibility": "implementation",
                "action_class": "software_implementation",
                "acceptance_test": "Focused tests pass",
                "authority_class": "routine",
                "parents": [],
            }
        ],
    }


def test_runtime_is_paused_and_killed_by_default(board):
    state = runtime_state(board)
    assert state["mode"] == "paused"
    assert state["kill_switch"] == 1
    assert state["daily_model_cost_ceiling_usd"] == 0


def test_semantic_signal_identity_deduplicates_new_evidence(board):
    first = record_signal(
        board,
        source_agent="chloe",
        expected_outcome="Stop presenting completed work as new",
        goal_ref="evernote:goal/proactive-workforce",
        observation="The board card is already complete",
        evidence_references=["kanban:event/1"],
        action_class="already_complete",
        target_ref="task-123",
    )
    second = record_signal(
        board,
        source_agent="brenna",
        expected_outcome="Stop presenting completed work as new",
        goal_ref="evernote:goal/proactive-workforce",
        observation="A later observation found the same completed card",
        evidence_references=["kanban:event/2"],
        action_class="already_complete",
        target_ref="task-123",
    )
    assert first["created"] is True
    assert first["status"] == "blocked"
    assert board.execute(
        "SELECT status FROM tasks WHERE id = ?", (first["task_id"],)
    ).fetchone()[0] == "blocked"
    assert kanban_db.recompute_ready(board) == 0
    assert board.execute(
        "SELECT status FROM tasks WHERE id = ?", (first["task_id"],)
    ).fetchone()[0] == "blocked"
    assert second["created"] is False
    assert first["task_id"] == second["task_id"]
    assert board.execute("SELECT COUNT(*) FROM wc_items WHERE item_kind='signal'").fetchone()[0] == 1


def test_only_aurora_can_plan_and_draft_creates_no_execution(board, organization):
    with pytest.raises(PermissionError, match="only Aurora"):
        record_plan(board, actor="emily", payload=plan_payload(), organization=organization)
    drafted = record_plan(board, actor="aurora", payload=plan_payload(), organization=organization)
    assert drafted["state"] == "draft"
    assert drafted["execution_cards_created"] == 0
    assert board.execute("SELECT COUNT(*) FROM wc_items WHERE item_kind='execution'").fetchone()[0] == 0


def test_technical_ownership_and_reserved_authority_are_enforced(board, organization):
    wrong_owner = plan_payload(nodes=[{
        "key": "implementation", "title": "Implement it", "assignee": "sage",
        "responsibility": "implementation", "action_class": "software_implementation",
        "acceptance_test": "Tests pass", "authority_class": "routine", "parents": [],
    }])
    with pytest.raises(ValueError, match="owned by sloane"):
        record_plan(board, actor="aurora", payload=wrong_owner, organization=organization)

    reserved = plan_payload(nodes=[{
        "key": "activation", "title": "Activate production", "assignee": "alina",
        "responsibility": "host_install_service_activation", "action_class": "activation",
        "acceptance_test": "Service is live", "authority_class": "reserved", "parents": [],
    }])
    plan = record_plan(board, actor="aurora", payload=reserved, organization=organization)
    set_runtime_mode(board, mode="apply", kill_switch=False, reason="isolated test")
    with pytest.raises(PermissionError, match="reserved-authority"):
        materialize_plan(
            board, actor="aurora", plan_id=plan["plan_id"],
            current_state_evidence=["copy://kanban/current"],
            current_state_evidence_at=int(time.time()), confirmed_execution_ready=True,
            organization=organization,
        )


def test_materialization_requires_activation_fresh_state_and_resolved_intake(board, organization):
    plan = record_plan(board, actor="aurora", payload=plan_payload(unresolved=["Elliott taste decision"]), organization=organization)
    with pytest.raises(RuntimeError, match="paused"):
        materialize_plan(
            board, actor="aurora", plan_id=plan["plan_id"],
            current_state_evidence=["copy://kanban/current"],
            current_state_evidence_at=int(time.time()), confirmed_execution_ready=True,
            organization=organization,
        )
    set_runtime_mode(board, mode="apply", kill_switch=False, reason="isolated test")
    with pytest.raises(ValueError, match="unresolved decisions"):
        materialize_plan(
            board, actor="aurora", plan_id=plan["plan_id"],
            current_state_evidence=["copy://kanban/current"],
            current_state_evidence_at=int(time.time()), confirmed_execution_ready=True,
            organization=organization,
        )


def test_bounded_graph_materializes_atomically_and_idempotently(board, organization):
    payload = plan_payload()
    payload["desired_outcome"] += " in the isolated fixture"
    plan = record_plan(board, actor="aurora", payload=payload, organization=organization)
    set_runtime_mode(board, mode="apply", kill_switch=False, reason="isolated test")
    first = materialize_plan(
        board, actor="aurora", plan_id=plan["plan_id"],
        current_state_evidence=["copy://kanban/current"],
        current_state_evidence_at=int(time.time()), confirmed_execution_ready=True,
        organization=organization,
    )
    second = materialize_plan(
        board, actor="aurora", plan_id=plan["plan_id"],
        current_state_evidence=["copy://kanban/current"],
        current_state_evidence_at=int(time.time()), confirmed_execution_ready=True,
        organization=organization,
    )
    assert first["created"] is True
    assert second == {"plan_id": plan["plan_id"], "root_task_id": first["root_task_id"], "created": False}
    assert len(first["execution_tasks"]) == 1
    root = kanban_db.get_task(board, first["root_task_id"])
    assert root is not None and root.status == "todo"


def test_failed_verification_reopens_outcome_and_creates_one_remediation(board, organization):
    outcome_id = kanban_db.create_task(board, title="Outcome under verification", assignee="aurora")
    verification_id = kanban_db.create_task(board, title="Verify outcome", assignee="reese")
    now = int(time.time())
    board.execute(
        "INSERT INTO wc_items(task_id,item_kind,stable_key,goal_ref,desired_outcome,acceptance_test,verification_state,current_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (outcome_id, "outcome", "fixture-outcome", "goal", "Verified result", "Tests pass", "pending", "open", now, now),
    )
    board.execute(
        "INSERT INTO wc_items(task_id,item_kind,stable_key,goal_ref,desired_outcome,acceptance_test,verification_state,current_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (verification_id, "verification", "fixture-verification", "goal", "Verify result", "Reproduce failure", "failed", "complete", now, now),
    )
    actions = propose_reconciliation(
        board, actor="reese", mode="proposed", organization=organization,
        observations=[{
            "task_id": verification_id, "target_task_id": outcome_id,
            "classification": "failed_verification", "confidence": "high",
            "rationale": "The acceptance test failed reproducibly",
            "evidence_references": ["test://failure/1"], "evidence_at": now,
        }],
    )
    set_runtime_mode(board, mode="apply", kill_switch=False, reason="isolated test")
    applied = apply_reconciliation(board, actor="aurora", action_ids=[actions[0]["action_id"]], organization=organization)
    assert applied[0]["state"] == "applied"
    assert kanban_db.get_task(board, outcome_id).status == "triage"
    remediation = board.execute("SELECT source_task_id FROM wc_relations WHERE relation='remediates' AND target_task_id=?", (outcome_id,)).fetchall()
    assert len(remediation) == 1


def test_unverified_outcome_is_quarantined_and_external_blockers_stay_blocked(board, organization):
    outcome_id = kanban_db.create_task(board, title="Unverified complete claim", assignee="aurora")
    blocked_id = kanban_db.create_task(board, title="Needs Elliott input", assignee="aurora")
    now = int(time.time())
    board.execute(
        "INSERT INTO wc_items(task_id,item_kind,stable_key,goal_ref,desired_outcome,verification_state,current_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (outcome_id, "outcome", "unverified-outcome", "goal", "Claimed result", "pending", "open", now, now),
    )
    actions = propose_reconciliation(
        board, actor="chloe", mode="proposed", organization=organization,
        observations=[
            {"task_id": outcome_id, "classification": "already_complete", "confidence": "high", "rationale": "A completion was claimed", "evidence_references": ["kanban://claim"], "evidence_at": now},
            {"task_id": blocked_id, "classification": "external_blocker", "confidence": "high", "rationale": "A retained decision is required", "evidence_references": ["decision://elliott"], "evidence_at": now},
        ],
    )
    set_runtime_mode(board, mode="apply", kill_switch=False, reason="isolated test")
    results = apply_reconciliation(board, actor="aurora", action_ids=[a["action_id"] for a in actions], organization=organization)
    assert results[0]["state"] == "quarantined"
    assert results[1]["state"] == "applied"
    blocked = kanban_db.get_task(board, blocked_id)
    assert blocked.status == "blocked" and blocked.block_kind == "needs_input"


def test_corrections_preserve_privacy_scope_and_dashboard_exposes_exceptions(board, organization):
    with pytest.raises(PermissionError, match="private relationship context"):
        record_correction(
            board, actor="aurora", classification="quality_standard", scope="workforce",
            description="Private relationship preference", provenance_ref="private://conversation",
            privacy_class="relationship_private", organization=organization,
        )
    correction = record_correction(
        board, actor="root", classification="workflow_defect", scope="system",
        description="Semantic identity must ignore changing observation prose",
        provenance_ref="test://semantic-dedupe", privacy_class="organizational",
        rule_target="plugins/workforce_control/store.py",
        regression_ref="tests/plugins/test_workforce_control.py",
        organization=organization,
    )
    assert correction["status"] == "implemented"
    snapshot = dashboard_snapshot(board)
    assert snapshot["runtime"]["mode"] in {"paused", "apply"}
    assert snapshot["corrections"]
    assert "exceptions" in snapshot


def test_dashboard_snapshot_is_read_only(board):
    before = board.execute(
        "SELECT updated_at FROM wc_schema WHERE singleton=1"
    ).fetchone()["updated_at"]
    snapshot = dashboard_snapshot(board)
    after = board.execute(
        "SELECT updated_at FROM wc_schema WHERE singleton=1"
    ).fetchone()["updated_at"]
    assert snapshot["available"] is True
    assert after == before


def test_observer_is_inert_while_paused_then_quarantines_unverified_completion(board, organization):
    outcome_id = kanban_db.create_task(board, title="Observer outcome", assignee="aurora")
    now = int(time.time())
    board.execute(
        "INSERT INTO wc_items(task_id,item_kind,stable_key,goal_ref,desired_outcome,verification_state,current_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (outcome_id, "outcome", "observer-outcome", "goal", "Observer result", "pending", "open", now, now),
    )
    with kanban_db.write_txn(board):
        board.execute("UPDATE tasks SET status='done',completed_at=? WHERE id=?", (now, outcome_id))
        kanban_db._append_event(board, outcome_id, "completed", {"fixture": True})
    assert observe_dispatch_tick(board, organization=organization)["paused"] is True
    assert board.execute("SELECT COUNT(*) FROM wc_reconcile_actions").fetchone()[0] == 0

    set_runtime_mode(board, mode="shadow", kill_switch=False, reason="isolated shadow test")
    observed = observe_dispatch_tick(board, organization=organization)
    assert observed["proposed"] == 1
    action = board.execute("SELECT state,classification FROM wc_reconcile_actions").fetchone()
    assert dict(action) == {"state": "shadow", "classification": "already_complete"}
