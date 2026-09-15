"""Fail-closed capability check for the internal handoff pickup CLI turn."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


_COORDINATION_ENV_KEYS = (
    "HERMES_COORDINATION_REQUEST_ROOT",
    "HERMES_COORDINATION_TASK_ID",
    "HERMES_COORDINATION_PURPOSE",
)

_PICKUP_ENV_KEYS = (
    "HERMES_WORKFORCE_HANDOFF_PICKUP_TASK",
    "HERMES_WORKFORCE_HANDOFF_PICKUP_TARGET",
    "HERMES_WORKFORCE_HANDOFF_PICKUP_SOURCE",
    "HERMES_WORKFORCE_HANDOFF_PICKUP_KIND",
    "HERMES_KANBAN_DB",
)

_PICKUP_MARKER_KEYS = (
    "HERMES_WORKFORCE_HANDOFF_PICKUP_TASK",
    "HERMES_WORKFORCE_HANDOFF_PICKUP_TARGET",
    "HERMES_WORKFORCE_HANDOFF_PICKUP_SOURCE",
    "HERMES_WORKFORCE_HANDOFF_PICKUP_KIND",
)

_ENV_KEYS = (*_COORDINATION_ENV_KEYS, *_PICKUP_ENV_KEYS)


def _scope_env() -> dict[str, str] | None:
    values = {key: os.environ.get(key, "") for key in _ENV_KEYS}
    # Coordination roots are inherited by ordinary repair and review workers.
    # Any pickup-only field opts a turn into this capability clamp.
    pickup_present = [key for key in _PICKUP_MARKER_KEYS if values[key]]
    if not pickup_present:
        return None
    if any(not values[key] for key in _PICKUP_ENV_KEYS):
        raise ValueError("pickup scope metadata is incomplete")
    coordination_present = [
        key for key in _COORDINATION_ENV_KEYS if values[key]
    ]
    if coordination_present and len(coordination_present) != len(
        _COORDINATION_ENV_KEYS
    ):
        raise ValueError("pickup coordination metadata is incomplete")
    claim_kind = values["HERMES_WORKFORCE_HANDOFF_PICKUP_KIND"]
    if claim_kind == "owned_operational_failure" and not coordination_present:
        raise ValueError("owned-failure pickup coordination metadata is missing")
    if claim_kind not in {"ordinary", "owned_operational_failure"}:
        raise ValueError("pickup claim kind is invalid")
    return values


def _canonical_agent(value: str) -> str:
    from hermes_cli.workforce_org import load_organization

    candidate = str(value or "").strip().casefold()
    if not candidate or candidate != str(value or "").strip():
        raise ValueError("pickup agent is not canonical")
    return load_organization().validate_execution_profile(candidate).agent


def _runtime_profile_agent(value: str) -> str:
    """Resolve one actual runtime name only through a declared profile path."""
    from hermes_cli.workforce_org import load_organization

    candidate = str(value or "").strip().casefold()
    if not candidate or candidate != str(value or "").strip():
        raise ValueError("pickup runtime profile is not canonical")
    organization = load_organization()
    declared = organization.from_profile_path(candidate)
    resolved = organization.validate_execution_profile(declared.agent)
    declared_profile = (
        Path(resolved.profile_path).name.casefold() if resolved.profile_path else ""
    )
    if declared_profile != candidate:
        raise ValueError("pickup runtime profile is not declared")
    return resolved.agent


def _active_profile_matches(target: str) -> bool:
    """Require the running profile, not only child-controlled scope fields."""
    from hermes_cli.profiles import get_active_profile_name

    try:
        return (
            _runtime_profile_agent(os.environ.get("HERMES_PROFILE", "")) == target
            and _runtime_profile_agent(get_active_profile_name()) == target
        )
    except Exception:
        return False


def _valid_identifier(value: str, prefix: str) -> bool:
    return (
        value.startswith(prefix)
        and len(value) <= 160
        and all(char.isascii() and (char.isalnum() or char in "_-") for char in value)
    )


def _payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ValueError("pickup payload is missing")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("pickup payload is invalid")
    return value


def _durable_claim_matches(scope: dict[str, str]) -> bool:
    """Read fresh task/event state instead of trusting child environment data."""
    from hermes_cli import kanban_db
    from hermes_cli.workforce_handoffs import (
        handoff_request_is_active,
        v1_handoff_creation_is_current,
    )

    db_path = Path(scope["HERMES_KANBAN_DB"])
    if not db_path.is_absolute() or not db_path.is_file():
        return False
    task_id = scope["HERMES_WORKFORCE_HANDOFF_PICKUP_TASK"]
    root_id = scope["HERMES_COORDINATION_REQUEST_ROOT"] or None
    claim_kind = scope["HERMES_WORKFORCE_HANDOFF_PICKUP_KIND"]
    target = _canonical_agent(scope["HERMES_WORKFORCE_HANDOFF_PICKUP_TARGET"])
    source = _canonical_agent(scope["HERMES_WORKFORCE_HANDOFF_PICKUP_SOURCE"])
    with kanban_db.connect_closing(db_path) as conn:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            return False
        body = _payload(task.body)
        context = body.get("context")
        owned_failure = bool(
            body.get("requires_source_acceptance") is True
            and isinstance(context, dict)
            and context.get("kind") == "owned_operational_failure"
        )
        if (
            body.get("kind") != "workforce_handoff"
            or body.get("state") != "pending_acknowledgment"
            or body.get("target_agent") != target
            or body.get("source_agent") != source
            or getattr(task, "request_root_id", None) != root_id
            or (claim_kind == "owned_operational_failure") != owned_failure
            or (
                claim_kind == "ordinary"
                and body.get("delivery_contract_version") != 1
            )
        ):
            return False
        if claim_kind == "ordinary" and not v1_handoff_creation_is_current(
            conn,
            task,
            body,
            source_agent=source,
            target_agent=target,
        ):
            return False
        if root_id and not handoff_request_is_active(conn, task):
            return False
        for event in reversed(kanban_db.list_events(conn, task_id)):
            if event.kind != "workforce_handoff_pickup_claimed":
                continue
            payload = event.payload
            return bool(
                isinstance(payload, dict)
                and payload.get("actor") == target
                and payload.get("target_agent") == target
                and payload.get("source_agent") == source
                and payload.get("request_root_id") == root_id
                and payload.get("claim_kind") == claim_kind
            )
    return False


def pickup_scope_denial(name: str, args: dict[str, Any]) -> str | None:
    """Return a reason when an internal pickup turn may not execute a tool."""
    try:
        scope = _scope_env()
        if scope is None:
            return None
        task_id = scope["HERMES_WORKFORCE_HANDOFF_PICKUP_TASK"]
        root_id = scope["HERMES_COORDINATION_REQUEST_ROOT"]
        coordination_task_id = scope["HERMES_COORDINATION_TASK_ID"]
        purpose = scope["HERMES_COORDINATION_PURPOSE"]
        claim_kind = scope["HERMES_WORKFORCE_HANDOFF_PICKUP_KIND"]
        has_coordination = bool(root_id)
        if (
            not _valid_identifier(task_id, "t_")
            or (has_coordination and not _valid_identifier(root_id, "cr_"))
            or (has_coordination and coordination_task_id != task_id)
            or (has_coordination and purpose != "work")
            or (not has_coordination and (coordination_task_id or purpose))
            or (claim_kind == "owned_operational_failure" and not has_coordination)
            or not _active_profile_matches(
                _canonical_agent(scope["HERMES_WORKFORCE_HANDOFF_PICKUP_TARGET"])
            )
            or not _durable_claim_matches(scope)
        ):
            return "workforce handoff pickup scope is invalid"
        if (
            name != "workforce_handoff"
            or not isinstance(args, dict)
            or args != {"action": "acknowledge", "task_id": task_id}
        ):
            return "only acknowledgment of the claimed workforce handoff is allowed"
        return None
    except Exception:
        return "workforce handoff pickup scope is invalid"
