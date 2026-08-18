"""Restricted tool surface for durable workforce handoffs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hermes_cli import kanban_db
from hermes_cli.workforce_handoffs import (
    acknowledge_handoff,
    create_handoff,
    record_checkpoint,
    sweep_overdue_handoffs,
)
from hermes_cli.workforce_org import load_organization
from tools.registry import registry, tool_error, tool_result


def _source() -> str:
    home = Path(os.environ.get("HERMES_HOME", "")).expanduser()
    if home.parent.name != "profiles":
        raise ValueError("workforce_handoff requires an active named profile")
    return load_organization().from_profile_path(home.name).agent


def _enabled() -> bool:
    try:
        return load_organization().validate_execution_profile(_source()).operational
    except Exception:
        return False


def _handle(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        actor = _source()
        action = str(args.get("action") or "")
        with kanban_db.connect_closing() as conn:
            if action == "create":
                result = create_handoff(
                    conn,
                    source_agent=actor,
                    target_agent=str(args.get("target_agent") or ""),
                    expected_outcome=str(args.get("expected_outcome") or ""),
                    acceptance_test=str(args.get("acceptance_test") or ""),
                    evidence_references=list(args.get("evidence_references") or []),
                    acknowledgment_deadline=str(args.get("acknowledgment_deadline") or ""),
                    checkpoint_at=str(args.get("checkpoint_at") or ""),
                )
            elif action == "acknowledge":
                result = acknowledge_handoff(
                    conn, str(args.get("task_id") or ""), actor=actor
                )
            elif action == "checkpoint":
                result = record_checkpoint(
                    conn,
                    str(args.get("task_id") or ""),
                    actor=actor,
                    evidence_references=list(args.get("evidence_references") or []),
                    next_checkpoint_at=args.get("next_checkpoint_at"),
                )
            elif action == "sweep":
                result = {"changed": sweep_overdue_handoffs(conn, actor=actor)}
            else:
                raise ValueError("action must be create, acknowledge, checkpoint, or sweep")
        return tool_result(success=True, action=action, result=result)
    except Exception as exc:
        return tool_error(str(exc))


WORKFORCE_HANDOFF_SCHEMA = {
    "name": "workforce_handoff",
    "description": "Create, explicitly acknowledge, checkpoint, or mechanically flag a durable workforce handoff.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "acknowledge", "checkpoint", "sweep"]},
            "task_id": {"type": "string"},
            "target_agent": {"type": "string"},
            "expected_outcome": {"type": "string"},
            "acceptance_test": {"type": "string"},
            "evidence_references": {"type": "array", "items": {"type": "string"}},
            "acknowledgment_deadline": {"type": "string"},
            "checkpoint_at": {"type": "string"},
            "next_checkpoint_at": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


registry.register(
    name="workforce_handoff",
    toolset="workforce",
    schema=WORKFORCE_HANDOFF_SCHEMA,
    handler=_handle,
    check_fn=_enabled,
    emoji="🤝",
)
