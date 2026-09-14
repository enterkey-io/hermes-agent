"""Behavioral contract for the task-worker-only Kanban lifecycle skill."""

from pathlib import Path


ROOT = Path(__file__).parents[2]
SKILL = ROOT / "skills" / "devops" / "kanban-workflows" / "SKILL.md"


def test_skill_is_kanban_only_and_documents_all_terminal_routes() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "name: kanban-workflows" in text
    assert "environments:" in text and "kanban" in text
    for route in (
        "done_and_handoff",
        "changes_needed",
        "stuck_and_escalate",
        "complete",
        "kanban_handoff",
        "kanban_pass_review",
        "kanban_request_changes",
        "kanban_complete",
    ):
        assert route in text


def test_skill_preserves_same_card_and_role_boundaries() -> None:
    text = SKILL.read_text(encoding="utf-8").lower()
    assert "never create a child" in text
    assert "ordinary lifecycle" in text
    assert "never complete another role's phase" in text
    assert "code qa" in text and "live acceptance" in text
    assert "never ask elliott to coordinate internal work" in text


def test_skill_requires_orientation_evidence_and_recheck_condition() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for required in (
        "kanban_show",
        "current_phase",
        "original_author",
        "technical_reviewer",
        "intent_validator",
        "activation_owner",
        "closure_owner",
        "expected outcome",
        "recheck condition",
    ):
        assert required in text


def test_operational_failures_require_owned_durable_action_before_prose() -> None:
    text = SKILL.read_text(encoding="utf-8").lower()
    assert "operational failure" in text
    assert "durable" in text
    assert "advisory" in text
    assert "retry" in text or "recover" in text
    assert "kanban_request_changes" in text
    assert "kanban_handoff" in text
    assert "kanban_block" in text
    assert "elliott" in text
