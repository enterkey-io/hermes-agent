"""Durable, organization-aware workforce handoffs backed by Hermes Kanban."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import sqlite3
import time
from typing import Any

from hermes_cli import kanban_db
from hermes_cli.sqlite_util import write_txn
from hermes_cli.workforce_org import WorkforceOrganization, load_organization


HANDOFF_KIND = "workforce_handoff"


def _timestamp(value: str) -> int:
    text = str(value or "").strip()
    if not text:
        raise ValueError("deadline is required")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("deadlines must include a timezone")
    return int(parsed.timestamp())


def _body(task) -> dict[str, Any]:
    try:
        value = json.loads(task.body or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("task is not a structured workforce handoff") from exc
    if not isinstance(value, dict) or value.get("kind") != HANDOFF_KIND:
        raise ValueError("task is not a workforce handoff")
    return value


def _has_creation_provenance(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    source_agent: str,
    target_agent: str,
    creation_binding: str,
) -> bool:
    matches = 0
    for event in kanban_db.list_events(conn, task_id):
        if event.kind != "workforce_handoff_created":
            continue
        event_payload = event.payload
        if (
            isinstance(event_payload, dict)
            and event_payload.get("actor") == source_agent
            and event_payload.get("target_agent") == target_agent
            and event_payload.get("creation_binding") == creation_binding
            and event_payload.get("delivery_contract_version") == 1
        ):
            matches += 1
    return matches == 1


def _creation_binding_digest(
    *,
    source_agent: str,
    target_agent: str,
    payload: dict[str, Any],
    idempotency_key: str,
    max_runtime_seconds: int | None,
    request_root_id: str | None,
    session_id: str | None,
    origin_message_id: str | None,
) -> str:
    binding = {
        "source_agent": source_agent,
        "target_agent": target_agent,
        "expected_outcome": payload.get("expected_outcome"),
        "acceptance_test": payload.get("acceptance_test"),
        "evidence_references": payload.get("evidence_references"),
        "acknowledgment_deadline": payload.get("acknowledgment_deadline"),
        "checkpoint_at": payload.get("checkpoint_at"),
        "requires_source_acceptance": payload.get("requires_source_acceptance"),
        "context": payload.get("context"),
        "idempotency_key": idempotency_key,
        "max_runtime_seconds": max_runtime_seconds,
        "request_root_id": request_root_id,
        "session_id": session_id,
        "origin_message_id": origin_message_id,
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(
            binding,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _origin_message_id(conn: sqlite3.Connection, task_id: str) -> str | None:
    created = next(
        (event for event in kanban_db.list_events(conn, task_id) if event.kind == "created"),
        None,
    )
    if created is None or not isinstance(created.payload, dict):
        return None
    return str(created.payload.get("coordination_origin_message_id") or "").strip() or None


def v1_handoff_creation_is_current(
    conn: sqlite3.Connection,
    task: Any,
    payload: dict[str, Any],
    *,
    source_agent: str,
    target_agent: str,
) -> bool:
    """Verify that a v1 handoff still matches its immutable creation."""
    creation_binding = str(payload.get("creation_binding") or "")
    if payload.get("delivery_contract_version") != 1 or not creation_binding:
        return False
    if creation_binding != _creation_binding_digest(
        source_agent=source_agent,
        target_agent=target_agent,
        payload=payload,
        idempotency_key=str(task.idempotency_key or ""),
        max_runtime_seconds=task.max_runtime_seconds,
        request_root_id=task.request_root_id,
        session_id=str(task.session_id or "").strip() or None,
        origin_message_id=_origin_message_id(conn, task.id),
    ):
        return False
    return _has_creation_provenance(
        conn,
        task.id,
        source_agent=source_agent,
        target_agent=target_agent,
        creation_binding=creation_binding,
    )


def _authorized_route(org: WorkforceOrganization, source: str, target: str) -> None:
    sender = org.validate_execution_profile(source)
    receiver = org.validate_execution_profile(target)
    if (
        sender.agent == "aurora"
        or receiver.manager == sender.agent
        or sender.manager == receiver.agent
        or {sender.agent, receiver.agent} == {"aurora", "grace"}
    ):
        return
    raise ValueError("cross-team handoffs and non-report assignments must route through Aurora")


def handoff_request_is_active(
    conn: sqlite3.Connection,
    task: kanban_db.Task,
    *,
    now: int | None = None,
) -> bool:
    """Return whether a linked handoff still has live request authority."""
    if not task.request_root_id:
        return True
    request = kanban_db.get_coordination_request(conn, task.request_root_id)
    timestamp = int(time.time() if now is None else now)
    return bool(
        request is not None
        and request.kind in kanban_db.WORKFORCE_HANDOFF_INHERITABLE_REQUEST_KINDS
        and request.status == "active"
        and timestamp < request.checkpoint_at
    )


def create_handoff(
    conn: sqlite3.Connection,
    *,
    source_agent: str,
    target_agent: str,
    expected_outcome: str,
    acceptance_test: str,
    evidence_references: list[str],
    acknowledgment_deadline: str,
    checkpoint_at: str,
    organization: WorkforceOrganization | None = None,
    context: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    requires_source_acceptance: bool = False,
    allow_overdue: bool = False,
    max_runtime_seconds: int | None = None,
    coordination_source_task_id: str | None = None,
    session_id: str | None = None,
    coordination_origin_message_id: str | None = None,
) -> dict[str, Any]:
    org = organization or load_organization()
    if not isinstance(requires_source_acceptance, bool):
        raise ValueError("requires_source_acceptance must be a boolean")
    _authorized_route(org, source_agent, target_agent)
    source = org.resolve_profile(source_agent).agent
    target = org.resolve_profile(target_agent).agent
    source_task_id = str(coordination_source_task_id or "").strip() or None
    origin_session_id = str(session_id or "").strip() or None
    origin_message_id = str(coordination_origin_message_id or "").strip() or None
    request_root_id = None
    if source_task_id:
        source_task = kanban_db.get_task(conn, source_task_id)
        if source_task is None:
            raise ValueError(f"unknown coordination source task: {source_task_id}")
        request_root_id = source_task.request_root_id
        if request_root_id:
            request = kanban_db.get_coordination_request(conn, request_root_id)
            if request is None:
                raise ValueError(f"unknown coordination request root: {request_root_id}")
            if (
                request.kind
                not in kanban_db.WORKFORCE_HANDOFF_INHERITABLE_REQUEST_KINDS
                or request.status != "active"
                or int(time.time()) >= request.checkpoint_at
            ):
                raise ValueError(
                    "inherited coordination request is unsupported or no longer active"
                )
            if (
                origin_session_id != request.origin_session_id
                or origin_message_id != request.origin_message_id
            ):
                raise ValueError(
                    "handoff origin must match its inherited coordination request"
                )
    ack_at = _timestamp(acknowledgment_deadline)
    checkpoint = _timestamp(checkpoint_at)
    if checkpoint <= ack_at:
        raise ValueError("checkpoint must be after the acknowledgment deadline")
    now = int(time.time())
    if ack_at <= now and not allow_overdue:
        raise ValueError("acknowledgment deadline must be in the future")
    payload: dict[str, Any] = {
        "kind": HANDOFF_KIND,
        "delivery_contract_version": 1,
        "state": "pending_acknowledgment",
        "source_agent": source,
        "target_agent": target,
        "expected_outcome": str(expected_outcome).strip(),
        "acceptance_test": str(acceptance_test).strip(),
        "evidence_references": list(evidence_references),
        "acknowledgment_deadline": ack_at,
        "checkpoint_at": checkpoint,
        "created_at": now,
        "notification_targets": ["aurora", "chloe"],
        "requires_source_acceptance": bool(requires_source_acceptance),
    }
    if context is not None:
        if not isinstance(context, dict):
            raise ValueError("context must be an object")
        payload["context"] = context
        if context.get("kind") == "owned_operational_failure":
            if not requires_source_acceptance:
                raise ValueError(
                    "owned operational failure handoffs require source acceptance"
                )
            if request_root_id:
                raise ValueError(
                    "owned operational failure handoffs cannot inherit a "
                    "coordination request"
                )
    if not payload["expected_outcome"] or not payload["acceptance_test"]:
        raise ValueError("expected_outcome and acceptance_test are required")
    handoff_key = idempotency_key or (
        f"workforce-handoff:{source}:{target}:"
        f"{ack_at}:{payload['expected_outcome']}"
    )
    normalized_max_runtime = (
        int(max_runtime_seconds) if max_runtime_seconds is not None else None
    )
    payload["creation_binding"] = _creation_binding_digest(
        source_agent=source,
        target_agent=target,
        payload=payload,
        idempotency_key=handoff_key,
        max_runtime_seconds=normalized_max_runtime,
        request_root_id=request_root_id,
        session_id=origin_session_id,
        origin_message_id=origin_message_id,
    )
    with kanban_db.write_txn(conn, allow_nested=True):
        if request_root_id:
            assert source_task_id is not None
            current_source = kanban_db.get_task(conn, source_task_id)
            current_request = kanban_db.get_coordination_request(
                conn, request_root_id
            )
            if (
                current_source is None
                or current_source.request_root_id != request_root_id
                or current_request is None
                or current_request.kind
                not in kanban_db.WORKFORCE_HANDOFF_INHERITABLE_REQUEST_KINDS
                or current_request.status != "active"
                or int(time.time()) >= current_request.checkpoint_at
                or origin_session_id != current_request.origin_session_id
                or origin_message_id != current_request.origin_message_id
            ):
                raise ValueError(
                    "inherited coordination request is unsupported or no longer active"
                )
        existing = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? "
            "AND status != 'archived' ORDER BY created_at DESC LIMIT 1",
            (handoff_key,),
        ).fetchone()
        task_id = kanban_db.create_task(
            conn,
            title=f"Handoff: {payload['expected_outcome'][:120]}",
            body=json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
            assignee=target,
            created_by=source,
            workspace_kind="scratch",
            triage=True,
            idempotency_key=handoff_key,
            max_runtime_seconds=normalized_max_runtime,
            coordination_source_task_id=source_task_id,
            session_id=origin_session_id,
            coordination_origin_message_id=origin_message_id,
        )
        task = kanban_db.get_task(conn, task_id)
        if task is None:  # pragma: no cover - create_task persistence invariant
            raise RuntimeError("workforce handoff did not persist")
        persisted = _body(task)
        try:
            persisted_assignee = org.validate_execution_profile(
                str(task.assignee or "")
            ).agent
            persisted_creator = org.validate_execution_profile(
                str(task.created_by or "")
            ).agent
        except ValueError as exc:
            raise ValueError("idempotent workforce handoff identity is invalid") from exc
        persisted_origin_message_id = _origin_message_id(conn, task_id)
        if (
            persisted.get("creation_binding") != payload["creation_binding"]
            or persisted_assignee != target
            or persisted_creator != source
            or task.request_root_id != request_root_id
            or (str(task.session_id or "").strip() or None) != origin_session_id
            or persisted_origin_message_id != origin_message_id
        ):
            raise ValueError(
                "idempotency key already belongs to a different workforce "
                "handoff or origin"
            )
        created = existing is None or existing["id"] != task_id
        if created:
            kanban_db._append_event(
                conn,
                task_id,
                "workforce_handoff_created",
                {
                    "actor": source,
                    "target_agent": target,
                    "creation_binding": payload["creation_binding"],
                    "delivery_contract_version": 1,
                },
            )
        if not v1_handoff_creation_is_current(
            conn,
            task,
            persisted,
            source_agent=source,
            target_agent=target,
        ):
            raise ValueError("idempotent workforce handoff provenance is invalid")
    return {
        "task_id": task_id,
        "request_root_id": task.request_root_id,
        "created": created,
        **persisted,
    }


def acknowledge_handoff(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    actor: str,
    organization: WorkforceOrganization | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    org = organization or load_organization()
    actor_id = org.validate_execution_profile(actor).agent
    task = kanban_db.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"unknown task {task_id}")
    payload = _body(task)
    try:
        target = org.validate_execution_profile(
            str(payload.get("target_agent") or "")
        ).agent
        source = org.validate_execution_profile(
            str(payload.get("source_agent") or "")
        ).agent
    except ValueError as exc:
        raise ValueError("workforce handoff identity is invalid") from exc
    if actor_id != target:
        raise ValueError("only the receiving agent can acknowledge a handoff")
    if payload["state"] != "pending_acknowledgment":
        raise ValueError(f"handoff cannot be acknowledged from {payload['state']}")
    context = payload.get("context")
    owned_failure = bool(
        payload.get("requires_source_acceptance") is True
        and isinstance(context, dict)
        and context.get("kind") == "owned_operational_failure"
    )
    if not owned_failure and payload.get("delivery_contract_version") == 1:
        try:
            assignee = org.validate_execution_profile(str(task.assignee or "")).agent
            creator = org.validate_execution_profile(str(task.created_by or "")).agent
        except ValueError as exc:
            raise ValueError("workforce handoff route is invalid") from exc
        if task.status != "triage" or assignee != target or creator != source:
            raise ValueError("workforce handoff route changed before acknowledgment")
        if not v1_handoff_creation_is_current(
            conn,
            task,
            payload,
            source_agent=source,
            target_agent=target,
        ):
            raise ValueError("workforce handoff contract changed before acknowledgment")
    with write_txn(conn):
        accepted_at = int(now if now is not None else time.time())
        if accepted_at > int(payload["acknowledgment_deadline"]):
            raise ValueError(
                "acknowledgment deadline has passed; Aurora must review the overdue handoff"
            )
        current_task = kanban_db.get_task(conn, task_id)
        if (
            current_task is None
            or current_task.status != task.status
            or current_task.request_root_id != task.request_root_id
            or _body(current_task) != payload
        ):
            raise ValueError("workforce handoff changed before acknowledgment")
        try:
            current_assignee = org.validate_execution_profile(
                str(current_task.assignee or "")
            ).agent
        except ValueError as exc:
            raise ValueError(
                "workforce handoff route changed before acknowledgment"
            ) from exc
        if current_assignee != target:
            raise ValueError("workforce handoff route changed before acknowledgment")
        if (
            not owned_failure
            and payload.get("delivery_contract_version") == 1
        ):
            try:
                current_creator = org.validate_execution_profile(
                    str(current_task.created_by or "")
                ).agent
            except ValueError as exc:
                raise ValueError(
                    "workforce handoff route changed before acknowledgment"
                ) from exc
            if current_creator != source:
                raise ValueError(
                    "workforce handoff route changed before acknowledgment"
                )
            if not v1_handoff_creation_is_current(
                conn,
                current_task,
                payload,
                source_agent=source,
                target_agent=target,
            ):
                raise ValueError(
                    "workforce handoff contract changed before acknowledgment"
                )
        if not handoff_request_is_active(conn, current_task, now=accepted_at):
            raise ValueError(
                "inherited coordination request is unsupported or no longer active"
            )
        accepted_payload = dict(payload)
        accepted_payload.update({
            "state": "accepted",
            "acknowledged_at": accepted_at,
        })
        conn.execute(
            "UPDATE tasks SET body = ?, status = 'ready' WHERE id = ?",
            (json.dumps(accepted_payload, indent=2, sort_keys=True), task_id),
        )
        kanban_db._append_event(
            conn, task_id, "workforce_handoff_acknowledged", {"actor": actor_id}
        )
    kanban_db.notify_task_updated(conn, task_id, ("body", "status"))
    return {"task_id": task_id, **accepted_payload}


def _claim_workforce_handoff_pickup(
    conn: sqlite3.Connection,
    *,
    target_agent: str,
    organization: WorkforceOrganization | None = None,
    now: int | None = None,
    owned_failure_only: bool = False,
) -> dict[str, Any] | None:
    """Claim one handoff acknowledgment for a silent receiver turn.

    The task body deliberately remains ``pending_acknowledgment``. Only the
    genuine target's later ``workforce_handoff(acknowledge)`` tool call may
    accept it. Owned failures retain their bounded internal request. Ordinary
    handoffs either inherit an existing user request or remain standalone; a
    request is never fabricated merely to make an internal handoff runnable.
    The one-shot event is the durable pickup claim, so failures after this
    commit stay owned and are handled by the existing overdue sweep.
    """
    org = organization or load_organization()
    target = org.validate_execution_profile(target_agent).agent
    claimed_at = int(now if now is not None else time.time())
    with kanban_db.write_txn(conn):
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status = 'triage' "
            "AND body LIKE ? ORDER BY created_at, id",
            ('%"kind": "workforce_handoff"%',),
        ).fetchall()
        for row in rows:
            task = kanban_db.get_task(conn, row["id"])
            if task is None:
                continue
            try:
                payload = _body(task)
                context = payload.get("context")
                payload_target = org.validate_execution_profile(
                    str(payload.get("target_agent") or "")
                ).agent
                source = org.validate_execution_profile(
                    str(payload.get("source_agent") or "")
                ).agent
                task_assignee = org.validate_execution_profile(
                    str(task.assignee or "")
                ).agent
                task_creator = org.validate_execution_profile(
                    str(task.created_by or "")
                ).agent
                _authorized_route(org, source, target)
                acknowledgment_deadline = payload.get("acknowledgment_deadline")
                checkpoint_at = payload.get("checkpoint_at")
                if (
                    not isinstance(acknowledgment_deadline, int)
                    or isinstance(acknowledgment_deadline, bool)
                    or not isinstance(checkpoint_at, int)
                    or isinstance(checkpoint_at, bool)
                ):
                    raise ValueError("handoff deadlines must be integer timestamps")
            except (TypeError, ValueError):
                continue
            owned_failure = bool(
                payload.get("requires_source_acceptance") is True
                and isinstance(context, dict)
                and context.get("kind") == "owned_operational_failure"
            )
            if (
                isinstance(context, dict)
                and context.get("kind") == "owned_operational_failure"
                and not owned_failure
            ):
                continue
            if not owned_failure and not v1_handoff_creation_is_current(
                conn,
                task,
                payload,
                source_agent=source,
                target_agent=target,
            ):
                continue
            if owned_failure_only and not owned_failure:
                continue
            if (
                payload.get("state") != "pending_acknowledgment"
                or payload_target != target
                or task_assignee != target
                or task_creator != source
                or claimed_at > acknowledgment_deadline
                or checkpoint_at <= acknowledgment_deadline
            ):
                continue
            already_claimed = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? "
                "AND kind = 'workforce_handoff_pickup_claimed' LIMIT 1",
                (task.id,),
            ).fetchone()
            if already_claimed is not None:
                continue
            request = None
            claim_kind = "ordinary"
            if owned_failure:
                try:
                    request = kanban_db.create_owned_failure_coordination_request(
                        conn,
                        root_task_id=task.id,
                        organization=org,
                        now=claimed_at,
                    )
                except ValueError:
                    # The factory performs the full context/assignee authority
                    # validation under a nested savepoint. A malformed earlier
                    # row must not prevent a later valid handoff from pickup.
                    continue
                claim_kind = "owned_operational_failure"
            elif task.request_root_id:
                request = kanban_db.get_coordination_request(
                    conn, task.request_root_id
                )
                if (
                    not handoff_request_is_active(conn, task, now=claimed_at)
                    or request is None
                ):
                    continue
            kanban_db._append_event(
                conn,
                task.id,
                "workforce_handoff_pickup_claimed",
                {
                    "actor": target,
                    "target_agent": target,
                    "source_agent": source,
                    "request_root_id": request.id if request is not None else None,
                    "claim_kind": claim_kind,
                    "claimed_at": claimed_at,
                },
            )
            return {
                "task_id": task.id,
                "target_agent": target,
                "source_agent": source,
                "request_root_id": request.id if request is not None else None,
                "claim_kind": claim_kind,
                "claimed_at": claimed_at,
            }
    return None


def claim_workforce_handoff_pickup(
    conn: sqlite3.Connection,
    *,
    target_agent: str,
    organization: WorkforceOrganization | None = None,
    now: int | None = None,
) -> dict[str, Any] | None:
    """Claim one ordinary or owned-failure handoff for its real receiver."""
    return _claim_workforce_handoff_pickup(
        conn,
        target_agent=target_agent,
        organization=organization,
        now=now,
    )


def claim_owned_failure_handoff_pickup(
    conn: sqlite3.Connection,
    *,
    target_agent: str,
    organization: WorkforceOrganization | None = None,
    now: int | None = None,
) -> dict[str, Any] | None:
    """Backward-compatible owned-failure-only pickup surface."""
    result = _claim_workforce_handoff_pickup(
        conn,
        target_agent=target_agent,
        organization=organization,
        now=now,
        owned_failure_only=True,
    )
    if result is not None:
        result.pop("claim_kind", None)
    return result


def record_checkpoint(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    actor: str,
    evidence_references: list[str],
    next_checkpoint_at: str | None = None,
    organization: WorkforceOrganization | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    org = organization or load_organization()
    actor_id = org.validate_execution_profile(actor).agent
    task = kanban_db.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"unknown task {task_id}")
    payload = _body(task)
    allowed = {payload["target_agent"], payload["source_agent"], "aurora"}
    if actor_id not in allowed:
        raise ValueError("only the receiver, sender, or Aurora may record a checkpoint")
    if payload["state"] not in {"accepted", "active"}:
        raise ValueError(f"checkpoint cannot be recorded from {payload['state']}")
    recorded_at = int(now if now is not None else time.time())
    payload["state"] = "active"
    payload["last_checkpoint_at"] = recorded_at
    payload["checkpoint_evidence"] = list(evidence_references)
    if next_checkpoint_at:
        next_at = _timestamp(next_checkpoint_at)
        if next_at <= recorded_at:
            raise ValueError("next checkpoint must be in the future")
        payload["checkpoint_at"] = next_at
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET body = ? WHERE id = ?",
            (json.dumps(payload, indent=2, sort_keys=True), task_id),
        )
        kanban_db._append_event(
            conn,
            task_id,
            "workforce_handoff_checkpoint",
            {"actor": actor_id, "evidence_count": len(evidence_references)},
        )
    kanban_db.notify_task_updated(conn, task_id, ("body",))
    return {"task_id": task_id, **payload}


def sweep_overdue_handoffs(
    conn: sqlite3.Connection,
    *,
    actor: str,
    organization: WorkforceOrganization | None = None,
    now: int | None = None,
) -> list[dict[str, Any]]:
    org = organization or load_organization()
    actor_id = org.validate_execution_profile(actor).agent
    if actor_id not in {"aurora", "chloe"}:
        raise ValueError("only Aurora or Chloe may perform the mechanical overdue sweep")
    current = int(now if now is not None else time.time())
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status NOT IN ('done','archived') AND body LIKE ?",
        ('%"kind": "workforce_handoff"%',),
    ).fetchall()
    changed: list[dict[str, Any]] = []
    for row in rows:
        task = kanban_db.get_task(conn, row["id"])
        if task is None:
            continue
        try:
            payload = _body(task)
            state_value = payload["state"]
            acknowledgment_deadline = int(payload["acknowledgment_deadline"])
            checkpoint_at = int(payload["checkpoint_at"])
        except (KeyError, TypeError, ValueError):
            continue

        # An owned-failure root in source review is not abandoned work. It may
        # wait beyond the ordinary checkpoint for distinct recovery executions,
        # then consume the request's reserved terminal source-review turn.
        if task.status == "review" and task.request_root_id:
            request = kanban_db.get_coordination_request(
                conn, task.request_root_id
            )
            if (
                request is not None
                and request.kind == "owned_operational_failure"
                and request.root_task_id == task.id
                and request.status in {"active", "return_pending"}
            ):
                continue

        if state_value == "pending_acknowledgment" and acknowledgment_deadline < current:
            state = "acknowledgment_overdue"
            event = "workforce_handoff_acknowledgment_overdue"
        elif (
            state_value in {"accepted", "active"}
            and checkpoint_at < current
        ):
            state = "stalled"
            event = "workforce_handoff_stalled"
        else:
            continue
        payload.update({"state": state, "flagged_at": current, "flagged_by": actor_id})
        reason = (
            "workforce handoff acknowledgment deadline passed"
            if state == "acknowledgment_overdue"
            else "workforce handoff checkpoint passed without a fresh checkpoint"
        )
        with write_txn(conn):
            conn.execute(
                "UPDATE tasks SET body = ?, status = 'blocked', "
                "block_kind = 'capability' WHERE id = ?",
                (json.dumps(payload, indent=2, sort_keys=True), task.id),
            )
            kanban_db._append_event(
                conn,
                task.id,
                event,
                {"actor": actor_id, "notify": ["aurora", "chloe"]},
            )
            kanban_db._append_event(
                conn,
                task.id,
                "blocked",
                {
                    "actor": actor_id,
                    "kind": "capability",
                    "reason": reason,
                    "handoff_state": state,
                },
            )
        kanban_db.notify_task_updated(conn, task.id, ("body", "status"))
        changed.append(
            {
                "task_id": task.id,
                "state": state,
                "notify": ["aurora", "chloe"],
                "decision_owner": "aurora",
            }
        )
    return changed
