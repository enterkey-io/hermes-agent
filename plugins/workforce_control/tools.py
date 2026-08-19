"""Agent-facing tools for controlled planning, reconciliation, and learning."""

from __future__ import annotations

from typing import Any

from hermes_cli import kanban_db
from hermes_cli.workforce_org import active_workforce_agent
from plugins.workforce_control.store import (
    apply_reconciliation,
    materialize_plan,
    propose_reconciliation,
    record_correction,
    record_plan,
    runtime_state,
)
from tools.registry import tool_error, tool_result


def _actor() -> str:
    return active_workforce_agent().agent


def _enabled() -> bool:
    try:
        return active_workforce_agent().operational
    except Exception:
        return False


def _plan(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        with kanban_db.connect_closing() as conn:
            result = record_plan(conn, actor=_actor(), payload=args)
        return tool_result(success=True, **result)
    except Exception as exc:
        return tool_error(str(exc))


def _materialize(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        with kanban_db.connect_closing() as conn:
            result = materialize_plan(
                conn, actor=_actor(), plan_id=str(args.get("plan_id") or ""),
                current_state_evidence=list(args.get("current_state_evidence") or []),
                current_state_evidence_at=args.get("current_state_evidence_at"),
                confirmed_execution_ready=bool(args.get("confirmed_execution_ready")),
            )
        return tool_result(success=True, **result)
    except Exception as exc:
        return tool_error(str(exc))


def _reconcile(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        action = str(args.get("action") or "propose")
        with kanban_db.connect_closing() as conn:
            if action == "status":
                result: Any = runtime_state(conn)
            elif action in {"shadow", "propose"}:
                result = propose_reconciliation(
                    conn, actor=_actor(), observations=list(args.get("observations") or []),
                    mode="shadow" if action == "shadow" else "proposed",
                )
            elif action == "apply":
                result = apply_reconciliation(conn, actor=_actor(), action_ids=list(args.get("action_ids") or []))
            else:
                raise ValueError("action must be status, shadow, propose, or apply")
        return tool_result(success=True, action=action, result=result)
    except Exception as exc:
        return tool_error(str(exc))


def _correct(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        with kanban_db.connect_closing() as conn:
            result = record_correction(
                conn, actor=_actor(), classification=str(args.get("classification") or ""),
                scope=str(args.get("scope") or ""), description=str(args.get("description") or ""),
                provenance_ref=str(args.get("provenance_ref") or ""),
                privacy_class=str(args.get("privacy_class") or "organizational"),
                rule_target=args.get("rule_target"), regression_ref=args.get("regression_ref"),
                supersedes_id=args.get("supersedes_id"),
            )
        return tool_result(success=True, **result)
    except Exception as exc:
        return tool_error(str(exc))


PLAN_SCHEMA = {
    "name": "workforce_plan", "description": "Draft one goal-aligned outcome and bounded execution graph without creating execution cards.",
    "parameters": {"type": "object", "properties": {
        "title": {"type": "string"}, "goal_ref": {"type": "string"}, "goal_evidence_at": {"type": "string"},
        "desired_outcome": {"type": "string"}, "acceptance_test": {"type": "string"},
        "priority_rationale": {"type": "string"}, "checkpoint": {"type": "string"},
        "capacity_assessment": {"type": "string"}, "deadline_dependencies": {"type": "string"},
        "displaced_work": {"type": "string"}, "unresolved_decisions": {"type": "array", "items": {"type": "string"}},
        "defer_or_stop": {"type": "string"}, "evidence_references": {"type": "array", "items": {"type": "string"}},
        "nodes": {"type": "array", "maxItems": 8, "items": {"type": "object"}},
    }, "required": ["title","goal_ref","desired_outcome","acceptance_test","priority_rationale","checkpoint","capacity_assessment","deadline_dependencies","displaced_work","unresolved_decisions","defer_or_stop","nodes"], "additionalProperties": False},
}

MATERIALIZE_SCHEMA = {
    "name": "workforce_materialize", "description": "After current-state revalidation, atomically materialize an approved Aurora draft. Reserved actions remain gated.",
    "parameters": {"type": "object", "properties": {
        "plan_id": {"type": "string"}, "current_state_evidence": {"type": "array", "items": {"type": "string"}},
        "current_state_evidence_at": {"type": "string"}, "confirmed_execution_ready": {"type": "boolean"},
    }, "required": ["plan_id","current_state_evidence","current_state_evidence_at","confirmed_execution_ready"], "additionalProperties": False},
}

RECONCILE_SCHEMA = {
    "name": "workforce_reconcile", "description": "Record shadow/proposed current-state dispositions or apply a high-confidence Aurora-approved transition.",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["status","shadow","propose","apply"]},
        "observations": {"type": "array", "items": {"type": "object"}},
        "action_ids": {"type": "array", "items": {"type": "string"}},
    }, "required": ["action"], "additionalProperties": False},
}

CORRECT_SCHEMA = {
    "name": "workforce_correct", "description": "Record a correction with provenance, privacy, scope, precedence, and regression linkage.",
    "parameters": {"type": "object", "properties": {
        "classification": {"type": "string"}, "scope": {"type": "string"}, "description": {"type": "string"},
        "provenance_ref": {"type": "string"}, "privacy_class": {"type": "string", "enum": ["organizational","personal_private","relationship_private"]},
        "rule_target": {"type": "string"}, "regression_ref": {"type": "string"}, "supersedes_id": {"type": "string"},
    }, "required": ["classification","scope","description","provenance_ref","privacy_class"], "additionalProperties": False},
}

TOOLS = (
    ("workforce_plan", PLAN_SCHEMA, _plan, _enabled, "🧭"),
    ("workforce_materialize", MATERIALIZE_SCHEMA, _materialize, _enabled, "🏗️"),
    ("workforce_reconcile", RECONCILE_SCHEMA, _reconcile, _enabled, "🔄"),
    ("workforce_correct", CORRECT_SCHEMA, _correct, _enabled, "🧠"),
)
