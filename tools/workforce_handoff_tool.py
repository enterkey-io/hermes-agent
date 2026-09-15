"""Restricted tool surface for durable workforce handoffs."""

from __future__ import annotations

from typing import Any

from hermes_cli import kanban_db
from hermes_cli.workforce_handoffs import (
    acknowledge_handoff,
    create_handoff,
    record_checkpoint,
    sweep_overdue_handoffs_across_boards,
)
from hermes_cli.workforce_org import active_workforce_agent
from tools.registry import registry, tool_error, tool_result


def _source() -> str:
    return active_workforce_agent().agent


def _enabled() -> bool:
    try:
        return active_workforce_agent().operational
    except Exception:
        return False


def _handle(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        actor = _source()
        action = str(args.get("action") or "")
        if action == "create":
            from agent.coordination_budget import (
                coordination_materialization_binding,
                current_coordination_db_path,
                register_uncoordinated_materialization,
            )
            from gateway.session_context import get_session_env
            from tools.async_delegation import _current_origin_session_id
            from tools.kanban_tools import _maybe_auto_subscribe

            with coordination_materialization_binding() as (
                coordination_context,
                coordination_origin,
            ):
                if (
                    coordination_context is not None
                    and coordination_context[2] != "work"
                ):
                    raise ValueError(
                        "workforce handoffs may only be created from coordination work"
                    )
                source_task_id = (
                    coordination_context[1]
                    if coordination_context is not None
                    else None
                )
                origin_session_id, origin_message_id = coordination_origin
                session_id = (
                    origin_session_id
                    or _current_origin_session_id()
                    or get_session_env("HERMES_SESSION_ID", "")
                    or None
                )
                has_live_return_origin = bool(
                    (
                        get_session_env("HERMES_SESSION_PLATFORM", "")
                        and get_session_env("HERMES_SESSION_CHAT_ID", "")
                    )
                    or get_session_env("HERMES_SESSION_KEY", "")
                )
                database_path = (
                    current_coordination_db_path()
                    if coordination_context is not None
                    else kanban_db.canonical_coordination_db_path()
                )
                if database_path is None:  # pragma: no cover - bound invariant
                    raise ValueError("coordination database binding is missing")
                with kanban_db.connect_closing(
                    database_path
                ) as conn:
                    if coordination_context is not None:
                        assert source_task_id is not None
                        request = kanban_db.get_coordination_request(
                            conn, coordination_context[0]
                        )
                        source_task = kanban_db.get_task(conn, source_task_id)
                        if (
                            request is None
                            or request.status != "active"
                            or source_task is None
                            or source_task.request_root_id != request.id
                        ):
                            raise ValueError(
                                "current coordination handoff source is no longer active"
                            )
                        session_id = request.origin_session_id
                        origin_message_id = request.origin_message_id
                    with kanban_db.write_txn(conn):
                        result = create_handoff(
                            conn,
                            source_agent=actor,
                            target_agent=str(args.get("target_agent") or ""),
                            expected_outcome=str(args.get("expected_outcome") or ""),
                            acceptance_test=str(args.get("acceptance_test") or ""),
                            evidence_references=list(
                                args.get("evidence_references") or []
                            ),
                            acknowledgment_deadline=str(
                                args.get("acknowledgment_deadline") or ""
                            ),
                            checkpoint_at=str(args.get("checkpoint_at") or ""),
                            coordination_source_task_id=source_task_id,
                            session_id=session_id,
                            coordination_origin_message_id=origin_message_id or None,
                        )
                        task = kanban_db.get_task(conn, result["task_id"])
                        if task is None:  # pragma: no cover - create_handoff invariant
                            raise RuntimeError("workforce handoff did not persist")
                        subscribed = False
                        if task.request_root_id is None:
                            subscribed = _maybe_auto_subscribe(
                                conn,
                                task.id,
                                explicit=True,
                                delivery_mode="wake",
                            )
                            if has_live_return_origin and not (
                                subscribed and task.session_id
                            ):
                                raise RuntimeError(
                                    "workforce handoff could not attach its source return route"
                                )
                    register_uncoordinated_materialization(
                        created=bool(result["created"]),
                        request_root_id=task.request_root_id,
                    )
                    result.update({
                        "session_id": task.session_id,
                        "subscribed": subscribed,
                        "wake_attached": bool(
                            task.request_root_id
                            or (subscribed and task.session_id)
                        ),
                        "delivery_mode": (
                            "request_final_return"
                            if task.request_root_id
                            else "session_wake"
                            if subscribed and task.session_id
                            else "none"
                        ),
                        "delivery_warning": (
                            None
                            if task.request_root_id
                            or (subscribed and task.session_id)
                            else "no persistent source return route is available"
                        ),
                    })
        else:
            if action == "sweep":
                result = {
                    "changed": sweep_overdue_handoffs_across_boards(actor=actor)
                }
                return tool_result(success=True, action=action, result=result)
            if action in {"acknowledge", "checkpoint"}:
                from agent.coordination_budget import current_coordination_db_path

                database_path = (
                    current_coordination_db_path() or kanban_db.kanban_db_path()
                )
            else:
                database_path = kanban_db.canonical_coordination_db_path()
            with kanban_db.connect_closing(
                database_path
            ) as conn:
                if action == "acknowledge":
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
