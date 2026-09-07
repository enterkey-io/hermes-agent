"""Same-card Kanban lifecycle contracts.

These tests intentionally exercise the durable DB API. Tool handlers and the
dispatcher are thin callers of these transitions, so the invariants belong at
the storage boundary where every surface shares them.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban_db as kb
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.workforce_org import load_organization
from tests.workforce_test_helpers import materialize_test_organization


ROOT = Path(__file__).parents[2]


@pytest.fixture
def lifecycle_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    org_path = materialize_test_organization(
        ROOT / "workforce" / "organization.yaml", tmp_path
    )
    data = yaml.safe_load(org_path.read_text(encoding="utf-8"))
    for item in data["agents"]:
        if not item.get("operational") or item.get("status") != "active":
            continue
        profile = Path(item["profile_path"])
        profile.joinpath("config.yaml").write_text(
            "model:\n  default: test-model\n  provider: test-provider\n",
            encoding="utf-8",
        )
        skills = profile / "skills"
        skills.mkdir(exist_ok=True)
        specialist = skills / "specialist"
        specialist.mkdir(exist_ok=True)
        specialist.joinpath("SKILL.md").write_text(
            "---\nname: specialist\ndescription: Test specialist.\n---\n# Specialist\n",
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
    return [json.loads(row["payload"]) if row["payload"] else {} for row in rows]


def _managed_task(conn: sqlite3.Connection, **overrides) -> str:
    values = {
        "title": "ship lifecycle",
        "body": "Acceptance: verified behavior.",
        "assignee": "sloane",
        "created_by": "aurora",
        "workspace_kind": "scratch",
        "lifecycle_type": "software",
        "original_author": "aurora",
        "implementer": "sloane",
        "technical_reviewer": "reese",
        "intent_validator": "aurora",
        "activation_owner": "alina",
        "closure_owner": "aurora",
        "current_phase": "execution",
        "return_to": "sloane",
        "skills": ["specialist"],
        "idempotency_key": "ship-lifecycle-v1",
    }
    values.update(overrides)
    return kb.create_task(conn, **values)


def _record_completion_evidence(conn: sqlite3.Connection, task_id: str) -> None:
    """Install the prior-phase evidence required by the closure gate."""
    kb._append_event(conn, task_id, "review_passed", {"evidence": {"qa": "pass"}})
    kb._append_event(
        conn,
        task_id,
        "handoff_created",
        {"source_phase": "intent_review", "evidence": {"intent": "pass"}},
    )
    kb._append_event(
        conn,
        task_id,
        "handoff_created",
        {"source_phase": "activation", "evidence": {"canary": "pass"}},
    )


def test_additive_migration_preserves_legacy_rows(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = home / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, "
        "assignee TEXT, status TEXT NOT NULL, priority INTEGER DEFAULT 0, "
        "created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER, "
        "completed_at INTEGER, workspace_kind TEXT NOT NULL DEFAULT 'scratch', "
        "workspace_path TEXT, claim_lock TEXT, claim_expires INTEGER)"
    )
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at) VALUES ('legacy','old','ready',1)"
    )
    conn.commit()
    conn.close()

    kb._INITIALIZED_PATHS.clear()
    kb.init_db(db)
    with kb.connect(db) as migrated:
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(tasks)")}
        assert {
            "original_author",
            "implementer",
            "technical_reviewer",
            "intent_validator",
            "activation_owner",
            "closure_owner",
            "current_phase",
            "lifecycle_type",
            "return_to",
        } <= columns
        task = kb.get_task(migrated, "legacy")
        assert task is not None
        assert task.lifecycle_type is None
        assert task.current_phase is None
        assert task.status == "ready"


def test_complete_same_card_software_lifecycle(lifecycle_env, monkeypatch) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(conn)
        created = kb.get_task(conn, task_id)
        assert created.original_author == "aurora"
        assert created.closure_owner == "aurora"

        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="Implemented and ran focused tests.",
            metadata={"tests_run": ["pytest focused"]},
            reviewer="reese",
            expected_run_id=implementation.current_run_id,
        )
        awaiting_review = kb.get_task(conn, task_id)
        assert awaiting_review.current_phase == "technical_review"
        assert awaiting_review.implementer == "sloane"
        assert awaiting_review.assignee == "reese"

        review = kb.claim_review_task(conn, task_id)
        assert review is not None
        ok, receiver = kb.pass_review(
            conn,
            task_id,
            summary="PASS: diff inspected and focused tests reproduced.",
            metadata={"review_checks": ["pytest focused"]},
            expected_run_id=review.current_run_id,
        )
        assert (ok, receiver) == (True, "aurora")
        intent_wait = kb.get_task(conn, task_id)
        assert intent_wait.current_phase == "intent_review"
        assert intent_wait.assignee == "aurora"

        intent = kb.claim_task(conn, task_id)
        assert intent is not None
        ok, receiver = kb.handoff_task(
            conn,
            task_id,
            next_assignee="alina",
            next_phase="activation",
            summary="Intent accepted against the approved outcome.",
            evidence={"acceptance_map": ["verified behavior"]},
            expected_outcome="Activate the reviewed revision locally.",
            recheck_condition="Service canary returns the expected result.",
            expected_run_id=intent.current_run_id,
        )
        assert (ok, receiver) == (True, "alina")

        activation = kb.claim_task(conn, task_id)
        assert activation is not None
        ok, receiver = kb.handoff_task(
            conn,
            task_id,
            next_assignee="aurora",
            next_phase="live_acceptance",
            summary="Activated reviewed revision with rollback captured.",
            evidence={"revision": "abc123", "rollback": "previous", "canary": "ok"},
            expected_outcome="Verify the live user-visible outcome.",
            recheck_condition="Live acceptance criteria remain green.",
            expected_run_id=activation.current_run_id,
        )
        assert (ok, receiver) == (True, "aurora")

        live = kb.claim_task(conn, task_id)
        assert live is not None
        assert kb.complete_task(
            conn,
            task_id,
            summary="Live outcome verified and accepted.",
            metadata={"live_evidence": ["canary ok", "user path verified"]},
            expected_run_id=live.current_run_id,
        )
        done = kb.get_task(conn, task_id)
        assert done.status == "done"
        assert done.original_author == "aurora"
        assert done.closure_owner == "aurora"
        assert done.current_phase == "closure"

        assert len(_events(conn, task_id, "review_passed")) == 1
        assert len(_events(conn, task_id, "handoff_created")) == 2
        assignments = _events(conn, task_id, "assigned")
        assert [event["assignee"] for event in assignments[-3:]] == [
            "aurora",
            "alina",
            "aurora",
        ]


def test_review_and_intent_failures_return_to_recorded_implementer(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(conn, idempotency_key="failures-v1")
        implementation = kb.claim_task(conn, task_id)
        assert implementation
        assert kb.request_review(
            conn,
            task_id,
            summary="Candidate ready.",
            metadata={"tests_run": ["focused"]},
            reviewer="reese",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id)
        assert review
        ok, receiver = kb.request_changes(
            conn,
            task_id,
            reason="The failure path does not preserve the prior value.",
            expected_run_id=review.current_run_id,
        )
        assert (ok, receiver) == (True, "sloane")
        assert kb.get_task(conn, task_id).current_phase == "execution"

        implementation2 = kb.claim_task(conn, task_id)
        assert implementation2
        assert kb.request_review(
            conn,
            task_id,
            summary="Failure path corrected and tested.",
            metadata={"tests_run": ["failure path"]},
            expected_run_id=implementation2.current_run_id,
        )
        review2 = kb.claim_review_task(conn, task_id)
        assert review2
        assert kb.pass_review(
            conn,
            task_id,
            summary="PASS after reproducing the corrected failure path.",
            metadata={"review_checks": ["failure test"]},
            expected_run_id=review2.current_run_id,
        ) == (True, "aurora")

        intent = kb.claim_task(conn, task_id)
        assert intent
        ok, receiver = kb.request_changes(
            conn,
            task_id,
            reason="The delivered command name differs from the approved interface.",
            expected_run_id=intent.current_run_id,
        )
        assert (ok, receiver) == (True, "sloane")
        task = kb.get_task(conn, task_id)
        assert task.current_phase == "execution"
        assert task.technical_reviewer == "reese"
        assert task.intent_validator == "aurora"


def test_lifecycle_review_reassignments_notify_after_commit(
    lifecycle_env, monkeypatch
) -> None:
    """Review handoffs wake the next same-card lifecycle owner post-commit."""
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    manager = get_plugin_manager()
    saved_hooks = {name: list(hooks) for name, hooks in manager._hooks.items()}
    observed: list[tuple[dict, tuple[str | None, str | None]]] = []

    def _capture(**kwargs) -> None:
        with sqlite3.connect(kb.kanban_db_path()) as observer_conn:
            row = observer_conn.execute(
                "SELECT assignee, current_phase FROM tasks WHERE id = ?",
                (kwargs["task_id"],),
            ).fetchone()
        observed.append((kwargs, tuple(row) if row else (None, None)))

    manager._hooks.setdefault("on_kanban_task_updated", []).append(_capture)
    try:
        with kb.connect() as conn:
            task_id = _managed_task(conn, idempotency_key="notify-review-v1")
            implementation = kb.claim_task(conn, task_id)
            assert implementation is not None
            observed.clear()

            assert kb.request_review(
                conn,
                task_id,
                summary="Implemented and ran focused tests.",
                metadata={"tests_run": ["focused"]},
                expected_run_id=implementation.current_run_id,
            )
            assert len(observed) == 1
            review_update, review_snapshot = observed.pop()
            assert review_update["changed_fields"] == [
                "status", "assignee", "current_phase", "return_to"
            ]
            assert review_update["assignee"] == "reese"
            assert review_snapshot == ("reese", "technical_review")

            review = kb.claim_review_task(conn, task_id)
            assert review is not None
            observed.clear()
            assert kb.request_changes(
                conn,
                task_id,
                reason="The failure path does not preserve the prior value.",
                expected_run_id=review.current_run_id,
            ) == (True, "sloane")
            assert len(observed) == 1
            changes_update, changes_snapshot = observed.pop()
            assert changes_update["changed_fields"] == [
                "status", "assignee", "current_phase", "return_to"
            ]
            assert changes_update["assignee"] == "sloane"
            assert changes_snapshot == ("sloane", "execution")
    finally:
        manager._hooks = saved_hooks


def test_handoff_retry_is_idempotent(lifecycle_env, monkeypatch) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn,
            lifecycle_type="research",
            assignee="sage",
            implementer="sage",
            technical_reviewer=None,
            activation_owner=None,
            intent_validator="emily",
            closure_owner="emily",
            return_to="sage",
            idempotency_key="research-v1",
        )
        run = kb.claim_task(conn, task_id)
        assert run
        kwargs = dict(
            next_assignee="emily",
            next_phase="intent_review",
            summary="Research complete with sources checked.",
            evidence={"sources": ["primary"]},
            expected_outcome="Validate evidence and synthesize.",
            recheck_condition="All material claims map to cited evidence.",
            expected_run_id=run.current_run_id,
        )
        assert kb.handoff_task(conn, task_id, **kwargs) == (True, "emily")
        assert kb.handoff_task(conn, task_id, **kwargs) == (True, "emily")
        assert len(_events(conn, task_id, "handoff_created")) == 1
        assert len(_events(conn, task_id, "assigned")) == 1


def test_handoff_tool_response_lost_retry_is_authorized_by_original_run(
    lifecycle_env, monkeypatch
) -> None:
    from tools import kanban_tools as kt

    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn,
            lifecycle_type="research",
            assignee="sage",
            implementer="sage",
            technical_reviewer=None,
            activation_owner=None,
            intent_validator="emily",
            closure_owner="emily",
            return_to="sage",
            idempotency_key="tool-retry-research-v1",
        )
        run = kb.claim_task(conn, task_id)
        assert run is not None

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setenv("HERMES_PROFILE", "sage")
    args = {
        "next_assignee": "emily",
        "next_phase": "intent_review",
        "summary": "Research complete with sources checked.",
        "evidence": {"sources": ["primary"]},
        "expected_outcome": "Validate evidence and synthesize.",
        "recheck_condition": "All material claims map to cited evidence.",
    }

    first = json.loads(kt._handle_handoff(args))
    retry = json.loads(kt._handle_handoff(args))

    assert first["ok"] is True
    assert retry["ok"] is True
    assert retry["next_assignee"] == "emily"
    with kb.connect() as conn:
        assert len(_events(conn, task_id, "handoff_created")) == 1
        assert len(_events(conn, task_id, "assigned")) == 1

    monkeypatch.delenv("HERMES_PROFILE")
    unidentified = json.loads(kt._handle_handoff(args))
    assert "ok" not in unidentified
    assert "cannot be verified" in unidentified["error"]

    # The successor owns the card now, but it does not own the ended Sage run.
    # Reusing that run id must not turn the retry path into an authorization bypass.
    monkeypatch.setenv("HERMES_PROFILE", "emily")
    refused = json.loads(kt._handle_handoff(args))
    assert "ok" not in refused
    assert "run" in refused["error"]


def test_pass_review_tool_response_lost_retry_is_authorized_by_original_run(
    lifecycle_env, monkeypatch
) -> None:
    from tools import kanban_tools as kt

    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn, title="review retry", idempotency_key="tool-review-retry-v1"
        )
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="Implementation ready for independent review.",
            metadata={"tests_run": ["focused"]},
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id)
        assert review is not None

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review.current_run_id))
    monkeypatch.setenv("HERMES_PROFILE", "reese")
    args = {
        "summary": "PASS after independent verification.",
        "evidence": {"tests": ["focused"]},
    }

    first = json.loads(kt._handle_pass_review(args))
    retry = json.loads(kt._handle_pass_review(args))

    assert first["ok"] is True
    assert retry["ok"] is True
    assert retry["next_assignee"] == "aurora"
    with kb.connect() as conn:
        assert len(_events(conn, task_id, "review_passed")) == 1

    monkeypatch.setenv("HERMES_PROFILE", "aurora")
    refused = json.loads(kt._handle_pass_review(args))
    assert "ok" not in refused
    assert "run" in refused["error"]


def test_enforcement_rejects_wrong_closer_and_missing_evidence(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(conn, idempotency_key="guards-v1")
        run = kb.claim_task(conn, task_id)
        assert run
        with pytest.raises(kb.LifecycleEnforcementError, match="closure owner"):
            kb.complete_task(
                conn,
                task_id,
                summary="Developer tried to close.",
                metadata={"live_evidence": ["none"]},
                expected_run_id=run.current_run_id,
            )
        assert kb.get_task(conn, task_id).status == "running"


def test_legacy_cards_keep_legacy_completion_when_enforcement_is_on(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="legacy", assignee="sloane")
        run = kb.claim_task(conn, task_id)
        assert run
        assert kb.complete_task(
            conn,
            task_id,
            summary="Legacy completion remains compatible.",
            expected_run_id=run.current_run_id,
        )
        assert kb.get_task(conn, task_id).status == "done"


def test_claim_preflight_rejects_missing_skill_before_run(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: False)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn,
            skills=["not-installed"],
            idempotency_key="missing-skill-v1",
        )
        monkeypatch.setattr(
            kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True
        )
        assert kb.claim_task(conn, task_id) is None
        assert kb.get_task(conn, task_id).status == "blocked"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == 0
        rejected = _events(conn, task_id, "lifecycle_preflight_failed")
        assert rejected and "skill" in rejected[-1]["reason"]


def test_claim_preflight_accepts_repo_bundled_skill(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: False)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn,
            skills=["sdlc-review"],
            idempotency_key="bundled-skill-v1",
        )
        monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
        assert kb.claim_task(conn, task_id) is not None


def test_claim_preflight_resolves_external_skill_relative_to_target_profile(
    lifecycle_env, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: False)
    profile = Path(load_organization().resolve_profile("sloane").profile_path)
    external_skill = profile / "relative-external" / "profile-external"
    external_skill.mkdir(parents=True)
    external_skill.joinpath("SKILL.md").write_text(
        "---\nname: profile-external\ndescription: Profile-relative fixture.\n---\n",
        encoding="utf-8",
    )
    profile.joinpath("config.yaml").write_text(
        "model:\n"
        "  default: test-model\n"
        "  provider: test-provider\n"
        "skills:\n"
        "  external_dirs:\n"
        "    - ${LIFECYCLE_EXTERNAL_SKILLS}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LIFECYCLE_EXTERNAL_SKILLS", "relative-external")
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    with kb.connect() as conn:
        task_id = _managed_task(
            conn,
            skills=["profile-external"],
            idempotency_key="relative-external-skill-v1",
        )
        monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
        assert kb.claim_task(conn, task_id) is not None


def test_completion_accepts_canonical_agent_and_profile_alias(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: False)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn,
            title="root alias closure",
            assignee="main",
            original_author="root",
            intent_validator="root",
            closure_owner="root",
            current_phase="closure",
            return_to="main",
            idempotency_key="root-alias-closure-v1",
        )
        with kb.write_txn(conn):
            _record_completion_evidence(conn, task_id)
        monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
        run = kb.claim_task(conn, task_id)
        assert run is not None
        assert kb.complete_task(
            conn,
            task_id,
            summary="Root accepted the live outcome through the main profile.",
            metadata={"live_evidence": ["verified"]},
            expected_run_id=run.current_run_id,
        )
        assert kb.get_task(conn, task_id).status == "done"


def test_completion_fails_closed_when_owner_identity_cannot_be_resolved(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: False)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn, title="unknown closure", idempotency_key="unknown-closure-v1"
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = 'unknown', closure_owner = 'unknown', "
                "current_phase = 'closure' WHERE id = ?",
                (task_id,),
            )
            _record_completion_evidence(conn, task_id)
        monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)

        with pytest.raises(kb.LifecycleEnforcementError, match="identity|ownership"):
            kb.complete_task(
                conn,
                task_id,
                summary="An unresolved identity must never close the card.",
                metadata={"live_evidence": ["untrusted"]},
            )
        assert kb.get_task(conn, task_id).status == "ready"


def test_pass_review_accepts_canonical_agent_and_profile_alias(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: False)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn,
            title="root alias review",
            technical_reviewer="root",
            idempotency_key="root-alias-review-v1",
        )
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="Ready for Root review.",
            metadata={"tests_run": ["focused"]},
            reviewer="root",
            expected_run_id=implementation.current_run_id,
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee = 'main' WHERE id = ?", (task_id,))
        review = kb.claim_review_task(conn, task_id)
        assert review is not None
        monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)

        assert kb.pass_review(
            conn,
            task_id,
            summary="PASS from the main profile.",
            metadata={"review_checks": ["focused"]},
            expected_run_id=review.current_run_id,
        ) == (True, "aurora")


def test_pass_review_fails_closed_when_reviewer_identity_cannot_be_resolved(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: False)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn, title="unknown reviewer", idempotency_key="unknown-reviewer-v1"
        )
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="Ready for review.",
            metadata={"tests_run": ["focused"]},
            expected_run_id=implementation.current_run_id,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = 'unknown', technical_reviewer = 'unknown' "
                "WHERE id = ?",
                (task_id,),
            )
        review = kb.claim_review_task(conn, task_id)
        assert review is not None
        monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)

        ok, reason = kb.pass_review(
            conn,
            task_id,
            summary="An unresolved reviewer must not pass.",
            metadata={"review_checks": ["untrusted"]},
            expected_run_id=review.current_run_id,
        )
        assert ok is False
        assert "identity" in reason
        assert kb.get_task(conn, task_id).status == "running"


def test_claim_preflight_accepts_supported_scalar_model_config(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: False)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn,
            skills=[],
            idempotency_key="scalar-model-v1",
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        profile = Path(load_organization().resolve_profile(task.assignee).profile_path)
        profile.joinpath("config.yaml").write_text(
            "model: test-provider/test-model\n", encoding="utf-8"
        )

        monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
        assert kb.claim_task(conn, task_id) is not None


def test_creation_preflight_rejects_duplicate_live_outcome(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        _managed_task(conn, idempotency_key="first-key")
        with pytest.raises(kb.LifecyclePreflightError, match="duplicate live"):
            _managed_task(conn, idempotency_key="second-key")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {
                "workspace_kind": "dir",
                "workspace_path": "/definitely/missing/lifecycle-workspace",
                "idempotency_key": "missing-workspace-v1",
            },
            "workspace",
        ),
        (
            {
                "assignee": "reese",
                "current_phase": "execution",
                "idempotency_key": "bad-phase-owner-v1",
            },
            "belongs to implementer",
        ),
    ],
)
def test_claim_preflight_rejects_invalid_workspace_and_role_before_run(
    lifecycle_env, monkeypatch, overrides, expected
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: False)
    with kb.connect() as conn:
        task_id = _managed_task(conn, **overrides)
        monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
        assert kb.claim_task(conn, task_id) is None
        assert kb.get_task(conn, task_id).status == "blocked"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == 0
        reason = _events(conn, task_id, "lifecycle_preflight_failed")[-1]["reason"]
        assert expected in reason


def test_lifecycle_creation_rejects_non_operational_actor(lifecycle_env) -> None:
    with kb.connect() as conn:
        with pytest.raises(Exception, match="cannot own or execute active work"):
            _managed_task(
                conn,
                title="artifact owner",
                assignee="elliott",
                implementer="elliott",
                idempotency_key="nonoperational-v1",
            )


def test_closure_requires_each_prior_phase_evidence(lifecycle_env, monkeypatch) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn, title="premature closure", idempotency_key="premature-close-v1"
        )
        conn.execute(
            "UPDATE tasks SET assignee = 'aurora', current_phase = 'closure' WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        run = kb.claim_task(conn, task_id)
        assert run
        with pytest.raises(kb.LifecycleEnforcementError, match="technical review PASS"):
            kb.complete_task(
                conn,
                task_id,
                summary="Attempted premature closure.",
                metadata={"live_evidence": ["only live evidence"]},
                expected_run_id=run.current_run_id,
            )


def test_deterministic_observer_routes_stale_assignment_once(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(conn, idempotency_key="stale-v1")
        conn.execute(
            "UPDATE tasks SET created_at = 1 WHERE id = ?", (task_id,)
        )
        conn.commit()
        first = kb.observe_lifecycle_handoffs(conn, now=10_000, stale_after_seconds=60)
        second = kb.observe_lifecycle_handoffs(conn, now=10_000, stale_after_seconds=60)
        assert first == [
            {
                "task_id": task_id,
                "kind": "stale_assignment",
                "manager": "emily",
            }
        ]
        assert second == []
        assert len(_events(conn, task_id, "lifecycle_manager_notified")) == 1


def test_deterministic_observer_routes_missing_assignee_via_recorded_implementer(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn, title="missing successor", idempotency_key="missing-successor-v1"
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = NULL, return_to = NULL WHERE id = ?",
                (task_id,),
            )

        result = kb.observe_lifecycle_handoffs(
            conn, now=10_000, stale_after_seconds=60
        )

        assert result == [
            {
                "task_id": task_id,
                "kind": "missing_successor",
                "manager": "emily",
            }
        ]
        notice = _events(conn, task_id, "lifecycle_manager_notified")[-1]
        assert notice["manager"] == "emily"


def test_lifecycle_tool_handlers_route_the_same_card(lifecycle_env, monkeypatch) -> None:
    """The model-facing tools expose the DB lifecycle without replacement cards."""
    from tools import kanban_tools as kt

    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(conn, idempotency_key="tool-loop-v1")
        run = kb.claim_task(conn, task_id)
        assert run
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setenv("HERMES_PROFILE", "sloane")

    review = json.loads(
        kt._handle_request_review(
            {"summary": "Implemented and verified.", "metadata": {"tests": ["focused"]}}
        )
    )
    assert review["ok"] is True

    with kb.connect() as conn:
        review_run = kb.claim_review_task(conn, task_id)
        assert review_run
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review_run.current_run_id))
    monkeypatch.setenv("HERMES_PROFILE", "reese")
    passed = json.loads(
        kt._handle_pass_review(
            {"summary": "PASS after independent verification.", "evidence": {"tests": ["focused"]}}
        )
    )
    assert passed["ok"] is True
    assert passed["next_assignee"] == "aurora"
    assert passed["next_phase"] == "intent_review"

    with kb.connect() as conn:
        intent_run = kb.claim_task(conn, task_id)
        assert intent_run
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(intent_run.current_run_id))
    monkeypatch.setenv("HERMES_PROFILE", "aurora")
    handed = json.loads(
        kt._handle_handoff(
            {
                "next_assignee": "alina",
                "next_phase": "activation",
                "summary": "Intent accepted.",
                "evidence": {"acceptance": "matched"},
                "expected_outcome": "Activate the reviewed revision.",
                "recheck_condition": "Canary is green.",
            }
        )
    )
    assert handed["ok"] is True
    assert handed["next_assignee"] == "alina"
    assert handed["next_phase"] == "activation"

    with kb.connect() as conn:
        cards = conn.execute("SELECT id FROM tasks").fetchall()
        assert [row["id"] for row in cards] == [task_id]


def test_create_tool_persists_lifecycle_provenance(lifecycle_env, monkeypatch) -> None:
    from tools import kanban_tools as kt

    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    monkeypatch.setenv("HERMES_PROFILE", "aurora")
    created = json.loads(
        kt._handle_create(
            {
                "title": "new managed outcome",
                "body": "Acceptance: the managed outcome is verified.",
                "assignee": "sloane",
                "skills": ["specialist"],
                "idempotency_key": "tool-create-lifecycle-v1",
                "lifecycle_type": "software",
                "original_author": "aurora",
                "implementer": "sloane",
                "technical_reviewer": "reese",
                "intent_validator": "aurora",
                "activation_owner": "alina",
                "closure_owner": "aurora",
                "current_phase": "execution",
                "return_to": "sloane",
            }
        )
    )
    assert created["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, created["task_id"])
        assert task.lifecycle_type == "software"
        assert task.original_author == "aurora"
        assert task.technical_reviewer == "reese"


def test_lifecycle_tool_schemas_are_registered() -> None:
    from tools import kanban_tools as kt

    assert kt.KANBAN_HANDOFF_SCHEMA["name"] == "kanban_handoff"
    assert kt.KANBAN_PASS_REVIEW_SCHEMA["name"] == "kanban_pass_review"
    assert set(kt.KANBAN_HANDOFF_SCHEMA["parameters"]["required"]) == {
        "next_assignee",
        "next_phase",
        "summary",
        "evidence",
        "expected_outcome",
        "recheck_condition",
    }
    assert set(
        kt.KANBAN_CREATE_SCHEMA["parameters"]["properties"]["lifecycle_type"]["enum"]
    ) == kb.VALID_LIFECYCLE_TYPES


def test_handoff_tool_rejects_a_caller_who_is_not_the_current_assignee(
    lifecycle_env, monkeypatch
) -> None:
    from tools import kanban_tools as kt

    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn, title="owned handoff", idempotency_key="owned-handoff-v1"
        )
        run = kb.claim_task(conn, task_id)
        assert run
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setenv("HERMES_PROFILE", "reese")

    response = json.loads(
        kt._handle_handoff(
            {
                "next_assignee": "aurora",
                "next_phase": "intent_review",
                "summary": "Attempted foreign handoff.",
                "evidence": {"claim": "not mine"},
                "expected_outcome": "Should not move.",
                "recheck_condition": "Assignee remains Sloane.",
            }
        )
    )
    assert "ok" not in response
    assert "current assignee" in response["error"]
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).assignee == "sloane"


def test_worker_context_includes_roles_bounded_history_and_latest_handoff(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn,
            lifecycle_type="research",
            assignee="sage",
            implementer="sage",
            technical_reviewer=None,
            activation_owner=None,
            intent_validator="emily",
            closure_owner="emily",
            return_to="sage",
            idempotency_key="context-v1",
        )
        run = kb.claim_task(conn, task_id)
        assert run
        assert kb.handoff_task(
            conn,
            task_id,
            next_assignee="emily",
            next_phase="intent_review",
            summary="Primary sources checked.",
            evidence={"sources": ["primary-a", "primary-b"]},
            expected_outcome="Validate the material claims.",
            recheck_condition="Every claim maps to source evidence.",
            expected_run_id=run.current_run_id,
        )[0]
        context = kb.build_worker_context(conn, task_id)

    assert "## Lifecycle assignment" in context
    assert "Original author: aurora" in context
    assert "Current phase: intent_review" in context
    assert "## Latest lifecycle handoff" in context
    assert "Primary sources checked." in context
    assert "## Lifecycle event history" in context
    assert "handoff_created" in context
    assert "assigned" in context


def test_worker_context_lifecycle_history_filters_noise_and_preserves_audit_evidence(
    lifecycle_env,
) -> None:
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        task_id = _managed_task(conn, idempotency_key="bounded-context-v1")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                "review_passed",
                {"summary": "Independent review evidence survives the cap."},
            )
            kb._append_event(
                conn,
                task_id,
                "handoff_created",
                {
                    "summary": "Latest handoff survives the cap.",
                    "evidence": {"handoff-check": "passed"},
                    "source_phase": "intent_review",
                    "next_phase": "activation",
                    "next_assignee": "alina",
                },
            )
            for index in range(40):
                kb._append_event(
                    conn,
                    task_id,
                    "assigned",
                    {"assignee": "sloane", "phase": "execution", "index": index},
                )
                for kind in ("heartbeat", "claimed", "reclaimed"):
                    kb._append_event(
                        conn,
                        task_id,
                        kind,
                        {"noise-marker": f"{kind}-{index}"},
                    )
        context = kb.build_worker_context(conn, task_id)

    show_payload = json.loads(kt._handle_show({"task_id": task_id}))

    assert "## Latest lifecycle handoff" in context
    assert "Latest handoff survives the cap." in context
    assert '"handoff-check": "passed"' in context
    assert "## Lifecycle event history" in context
    assert "Independent review evidence survives the cap." in context
    assert "earlier lifecycle events omitted" in context
    assert "assigned=" in context
    assert context.count(" assigned ") < 40
    assert "noise-marker" not in context
    assert len(show_payload["events"]) == 20
    assert show_payload["events_omitted"] == {
        "total": 23,
        "by_kind": {"assigned": 23},
    }
    assert "noise-marker" not in json.dumps(show_payload["events"])
    assert any(
        event["kind"] == "review_passed"
        and event["payload"]["summary"]
        == "Independent review evidence survives the cap."
        for event in show_payload["events"]
    )


def test_exception_manager_notifications_are_deduplicated_and_healthy_handoffs_are_quiet(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        healthy_id = _managed_task(
            conn, title="healthy notify", idempotency_key="healthy-notify-v1"
        )
        healthy_run = kb.claim_task(conn, healthy_id)
        assert healthy_run
        assert kb.request_review(
            conn,
            healthy_id,
            summary="Verified implementation.",
            metadata={"tests_run": ["focused"]},
            expected_run_id=healthy_run.current_run_id,
        )
        assert _events(conn, healthy_id, "lifecycle_manager_notified") == []

        stuck_id = _managed_task(
            conn, title="stuck notify", idempotency_key="stuck-notify-v1"
        )
        stuck_run = kb.claim_task(conn, stuck_id)
        assert stuck_run
        assert kb.block_task(
            conn,
            stuck_id,
            reason="External credential approval is required.",
            kind="needs_input",
            expected_run_id=stuck_run.current_run_id,
        )
        notices = _events(conn, stuck_id, "lifecycle_manager_notified")
        assert [notice["exception_kind"] for notice in notices] == ["stuck"]
        assert notices[0]["manager"] == "emily"
        assert notices[0]["model_calls"] == 0

        failed_id = _managed_task(
            conn, title="giveup notify", idempotency_key="giveup-notify-v1"
        )
        assert kb.claim_task(conn, failed_id)
        assert kb._record_task_failure(
            conn,
            failed_id,
            "worker could not start",
            outcome="spawn_failed",
            failure_limit=1,
            release_claim=True,
            end_run=True,
        )
        notices = _events(conn, failed_id, "lifecycle_manager_notified")
        assert [notice["exception_kind"] for notice in notices] == ["gave_up"]


def test_lifecycle_review_request_requires_structured_evidence(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn, title="review evidence", idempotency_key="review-evidence-v1"
        )
        run = kb.claim_task(conn, task_id)
        assert run
        ok, reason = kb.request_review(
            conn,
            task_id,
            summary="Claims implementation is ready.",
            metadata=None,
            expected_run_id=run.current_run_id,
            with_reason=True,
        )
        assert ok is False
        assert "evidence" in reason
        assert kb.get_task(conn, task_id).status == "running"


def test_recovery_handoff_must_follow_source_owners_stuck_route(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn, title="recovery route", idempotency_key="recovery-route-v1"
        )
        run = kb.claim_task(conn, task_id)
        assert run
        rejected = kb.handoff_task(
            conn,
            task_id,
            next_assignee="reese",
            next_phase="recovery",
            summary="Implementation is stuck.",
            evidence={"failed_step": "dependency resolution"},
            expected_outcome="Resolve the ownership blocker.",
            recheck_condition="The developer can resume safely.",
            expected_run_id=run.current_run_id,
        )
        assert rejected[0] is False
        assert "stuck route" in rejected[1]
        assert kb.get_task(conn, task_id).status == "running"


def test_recovery_handoff_to_technical_review_uses_review_lane_and_can_pass(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn,
            title="recovered implementation review",
            idempotency_key="recovery-review-pass-v1",
        )
        implementation_run = kb.claim_task(conn, task_id)
        assert implementation_run
        assert kb.handoff_task(
            conn,
            task_id,
            next_assignee="emily",
            next_phase="recovery",
            summary="Implementation needs an internal recovery step.",
            evidence={"failed_step": "dependency resolution"},
            expected_outcome="Restore a reviewable implementation.",
            recheck_condition="The focused implementation checks pass.",
            expected_run_id=implementation_run.current_run_id,
        ) == (True, "emily")

        recovery_run = kb.claim_task(conn, task_id)
        assert recovery_run
        assert kb.handoff_task(
            conn,
            task_id,
            next_assignee="reese",
            next_phase="technical_review",
            summary="The implementation was recovered and reverified.",
            evidence={"tests": ["focused lifecycle regression"]},
            expected_outcome="Perform independent technical review.",
            recheck_condition="Review evidence independently confirms the fix.",
            expected_run_id=recovery_run.current_run_id,
        ) == (True, "reese")

        awaiting_review = kb.get_task(conn, task_id)
        assert awaiting_review.status == "review"
        assert awaiting_review.current_phase == "technical_review"

        review_run = kb.claim_review_task(conn, task_id)
        assert review_run
        assert kb.pass_review(
            conn,
            task_id,
            summary="PASS after independent verification.",
            metadata={"tests": ["focused lifecycle regression"]},
            expected_run_id=review_run.current_run_id,
        ) == (True, "aurora")
        intent = kb.get_task(conn, task_id)
        assert intent.status == "ready"
        assert intent.current_phase == "intent_review"
        assert intent.assignee == "aurora"


@pytest.mark.parametrize("caller_metadata", [None, {}])
def test_request_review_tool_rejects_empty_caller_evidence_before_session_stamp(
    lifecycle_env, monkeypatch, caller_metadata
) -> None:
    from tools import kanban_tools as kt

    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn,
            title="empty tool evidence",
            idempotency_key="empty-tool-evidence-v1",
        )
        run = kb.claim_task(conn, task_id)
        assert run

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setenv("HERMES_PROFILE", "sloane")
    monkeypatch.setenv("HERMES_SESSION_ID", "worker-session-bookkeeping")

    args = {"summary": "Claims implementation is ready."}
    if caller_metadata is not None:
        args["metadata"] = caller_metadata
    result = json.loads(kt._handle_request_review(args))

    assert result.get("ok") is not True
    assert "evidence" in result["error"]
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "running"
        assert task.current_phase == "execution"


def test_completion_rechecks_lifecycle_ownership_inside_write_transaction(
    lifecycle_env, monkeypatch
) -> None:
    """A concurrent reassignment cannot race the pre-transaction closure gate."""
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn, title="closure race", idempotency_key="closure-race-v1"
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = 'aurora', current_phase = 'closure' "
                "WHERE id = ?",
                (task_id,),
            )
            kb._append_event(conn, task_id, "review_passed", {"evidence": {"qa": "pass"}})
            kb._append_event(
                conn,
                task_id,
                "handoff_created",
                {"source_phase": "intent_review", "evidence": {"intent": "pass"}},
            )
            kb._append_event(
                conn,
                task_id,
                "handoff_created",
                {"source_phase": "activation", "evidence": {"canary": "pass"}},
            )
        run = kb.claim_task(conn, task_id)
        assert run

        original_merge = kb._merge_completion_prose_artifacts

        def race_reassignment(*args, **kwargs):
            with kb.connect() as competing:
                with kb.write_txn(competing):
                    competing.execute(
                        "UPDATE tasks SET assignee = 'reese' WHERE id = ?",
                        (task_id,),
                    )
            return original_merge(*args, **kwargs)

        monkeypatch.setattr(kb, "_merge_completion_prose_artifacts", race_reassignment)
        with pytest.raises(kb.LifecycleEnforcementError, match="closure owner"):
            kb.complete_task(
                conn,
                task_id,
                summary="Attempted close across a reassignment race.",
                metadata={"live_evidence": ["verified"]},
                expected_run_id=run.current_run_id,
            )
        assert kb.get_task(conn, task_id).status == "running"


def test_handoff_receiver_is_dispatched_once_with_mandatory_lifecycle_skill(
    lifecycle_env, monkeypatch
) -> None:
    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda *_a, **_k: "ok")
    monkeypatch.setattr(kb, "count_running_tasks_other_boards", lambda *_a, **_k: 0)
    monkeypatch.setattr(kb, "_resolve_dispatch_profile", lambda value: value)
    spawned: list[tuple[str, str, list[str]]] = []

    def capture_spawn(task, workspace):
        spawned.append((task.id, task.assignee, list(task.skills or [])))
        return None

    with kb.connect() as conn:
        task_id = _managed_task(
            conn,
            title="research receiver wake",
            lifecycle_type="research",
            assignee="sage",
            implementer="sage",
            technical_reviewer=None,
            intent_validator="emily",
            activation_owner=None,
            closure_owner="emily",
            return_to="sage",
            idempotency_key="receiver-wake-v1",
        )
        run = kb.claim_task(conn, task_id)
        assert run
        assert kb.handoff_task(
            conn,
            task_id,
            next_assignee="emily",
            next_phase="intent_review",
            summary="Research evidence is ready for validation.",
            evidence={"sources": ["primary"]},
            expected_outcome="Validate the evidence.",
            recheck_condition="Every claim remains source-backed.",
            expected_run_id=run.current_run_id,
        ) == (True, "emily")

        first = kb.dispatch_once(
            conn, spawn_fn=capture_spawn, reconcile_orphans=False
        )
        second = kb.dispatch_once(
            conn, spawn_fn=capture_spawn, reconcile_orphans=False
        )

        assert [item[0] for item in first.spawned] == [task_id], first
        assert second.spawned == []
        assert spawned == [
            (task_id, "emily", ["kanban-workflows", "specialist"])
        ]


def test_lifecycle_safety_controls_default_off() -> None:
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["kanban"]["lifecycle_enforcement"] is False
    assert DEFAULT_CONFIG["kanban"]["lifecycle_observer"] is False


def test_complete_tool_rejects_runtime_profile_that_is_not_closure_owner(
    lifecycle_env, monkeypatch
) -> None:
    from tools import kanban_tools as kt

    monkeypatch.setattr(kb, "lifecycle_enforcement_enabled", lambda *_a, **_k: True)
    with kb.connect() as conn:
        task_id = _managed_task(
            conn, title="runtime closer", idempotency_key="runtime-closer-v1"
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = 'aurora', current_phase = 'closure' "
                "WHERE id = ?",
                (task_id,),
            )
            kb._append_event(conn, task_id, "review_passed", {"evidence": {"qa": "pass"}})
            kb._append_event(
                conn,
                task_id,
                "handoff_created",
                {"source_phase": "intent_review", "evidence": {"intent": "pass"}},
            )
            kb._append_event(
                conn,
                task_id,
                "handoff_created",
                {"source_phase": "activation", "evidence": {"canary": "pass"}},
            )
        run = kb.claim_task(conn, task_id)
        assert run

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.current_run_id))
    monkeypatch.setenv("HERMES_PROFILE", "reese")
    result = json.loads(
        kt._handle_complete(
            {
                "summary": "Foreign runtime attempted closure.",
                "metadata": {"live_evidence": ["verified"]},
            }
        )
    )

    assert "current assignee" in result["error"]
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "running"
