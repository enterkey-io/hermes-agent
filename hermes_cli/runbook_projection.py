"""Project canonical runbook metadata into the workflow registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hermes_cli import runbook_store
from hermes_cli import workflow_registry as registry
from hermes_cli.workflow_models import WorkflowConflictError, WorkflowNotFoundError


def _step_records(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, step in enumerate(metadata["steps"]):
        records.append(
            {
                "step_key": step["step_key"],
                "position": int(step.get("position", index)),
                "name": step["name"],
                "description": step.get("description"),
                "executor_profile": step.get("executor_profile"),
                "runtime_kind": step.get("runtime_kind") or metadata["runtime"]["kind"],
                "runtime_ref": step.get("runtime_ref"),
                "input_contract": step.get("inputs"),
                "output_contract": step.get("outputs"),
                "approval_policy": step.get("approval_policy"),
                "timeout_seconds": step.get("timeout_seconds"),
                "max_attempts": step.get("max_attempts"),
            }
        )
    return records


def project_runbook(record: runbook_store.RunbookRecord) -> dict[str, Any]:
    """Create or update a registry projection without mutating schedules."""
    metadata = runbook_store.read_runbook(Path(record.path)).metadata
    runtime = metadata["runtime"]
    timeout = metadata.get("timeout")
    retry = metadata.get("retry")
    dedupe = metadata.get("deduplication")
    values = {
        "id": metadata["id"],
        "slug": metadata["slug"],
        "name": metadata["title"],
        "description": metadata["purpose"],
        "owner_profile": metadata["owner_profile"],
        "status": metadata["status"],
        "runtime_kind": runtime["kind"],
        "runtime_ref": runtime.get("ref"),
        "source_path": record.path,
        "source_hash": record.source_hash,
        "source_revision": record.revision,
        "dedupe_strategy": dedupe.get("strategy") if isinstance(dedupe, dict) else None,
        "timeout_seconds": timeout.get("seconds") if isinstance(timeout, dict) else None,
        "max_attempts": retry.get("max_attempts") if isinstance(retry, dict) else None,
    }
    with registry.connect_closing() as conn:
        try:
            definition = registry.get_definition(conn, metadata["id"])
            changes = {
                key: value
                for key, value in values.items()
                if key != "id" and getattr(definition, key) != value
            }
            if changes:
                definition = registry.update_definition(
                    conn,
                    metadata["id"],
                    expected_version=definition.version,
                    **changes,
                )
        except WorkflowNotFoundError:
            try:
                definition = registry.create_definition(conn, **values)
            except WorkflowConflictError:
                existing = registry.get_definition_by_slug(conn, metadata["slug"])
                definition = registry.update_definition(
                    conn,
                    existing.id,
                    expected_version=existing.version,
                    **{key: value for key, value in values.items() if key != "id"},
                )
        registry.replace_steps(conn, definition.id, _step_records(metadata))
        for schedule in metadata.get("schedules", []):
            if not isinstance(schedule, dict) or not schedule.get("cron_job_id"):
                continue
            registry.link_schedule(
                conn,
                definition.id,
                profile=str(schedule.get("profile") or metadata["owner_profile"]),
                cron_job_id=str(schedule["cron_job_id"]),
                enabled=bool(schedule.get("enabled", True)),
            )
        result = registry.get_definition(conn, definition.id).to_dict()
        result["steps"] = [step.to_dict() for step in registry.list_steps(conn, definition.id)]
        return result


def snapshot_projection(conn, *, workflow_id: str, slug: str) -> dict[str, Any]:
    """Capture just one workflow projection for guarded compensation."""
    row = conn.execute(
        "SELECT * FROM workflow_definitions WHERE id = ? OR slug = ? ORDER BY id = ? DESC LIMIT 1",
        (workflow_id, slug, workflow_id),
    ).fetchone()
    if row is None:
        return {"definition": None, "steps": [], "schedules": []}
    definition = dict(row)
    resolved_id = str(definition["id"])
    return {
        "definition": definition,
        "steps": [
            dict(item)
            for item in conn.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id = ? ORDER BY position", (resolved_id,)
            )
        ],
        "schedules": [
            dict(item)
            for item in conn.execute(
                "SELECT * FROM workflow_schedules WHERE workflow_id = ?", (resolved_id,)
            )
        ],
    }


def project_runbook_transaction(
    conn, record: runbook_store.RunbookRecord, metadata: dict[str, Any]
) -> dict[str, Any]:
    """Project a canonical record using the caller's already-open transaction.

    This deliberately emits no incidental registry events: the enclosing
    activation records exactly one database-enforced terminal event.
    """
    runtime = metadata["runtime"]
    timeout = metadata.get("timeout")
    retry = metadata.get("retry")
    dedupe = metadata.get("deduplication")
    values = {
        "id": metadata["id"],
        "slug": metadata["slug"],
        "name": metadata["title"],
        "description": metadata["purpose"],
        "owner_profile": metadata["owner_profile"],
        "status": metadata["status"],
        "runtime_kind": runtime["kind"],
        "runtime_ref": runtime.get("ref"),
        "source_path": record.path,
        "source_hash": record.source_hash,
        "source_revision": record.revision,
        "kanban_board": None,
        "repair_task_id": None,
        "dedupe_strategy": dedupe.get("strategy") if isinstance(dedupe, dict) else None,
        "timeout_seconds": timeout.get("seconds") if isinstance(timeout, dict) else None,
        "max_attempts": retry.get("max_attempts") if isinstance(retry, dict) else None,
    }
    registry._validate_definition_fields(values)
    row = conn.execute("SELECT * FROM workflow_definitions WHERE id = ?", (values["id"],)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM workflow_definitions WHERE slug = ?", (values["slug"],)).fetchone()
    now = registry._now()
    if row is None:
        columns = [*values, "version", "created_at", "updated_at", "retired_at"]
        inserted = {**values, "version": 1, "created_at": now, "updated_at": now, "retired_at": None}
        conn.execute(
            f"INSERT INTO workflow_definitions({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [inserted[column] for column in columns],
        )
        workflow_id = str(values["id"])
    else:
        existing = dict(row)
        workflow_id = str(existing["id"])
        if workflow_id != values["id"]:
            raise WorkflowConflictError("workflow slug is already bound to a different id")
        changes = {key: value for key, value in values.items() if key != "id" and existing[key] != value}
        if changes:
            assignments = [f"{key} = ?" for key in changes]
            conn.execute(
                f"UPDATE workflow_definitions SET {', '.join(assignments)}, updated_at = ?, version = version + 1 WHERE id = ?",
                [*changes.values(), now, workflow_id],
            )
    conn.execute("DELETE FROM workflow_steps WHERE workflow_id = ?", (workflow_id,))
    for step in _step_records(metadata):
        conn.execute(
            """
            INSERT INTO workflow_steps(
                id, workflow_id, step_key, position, name, description,
                executor_profile, runtime_kind, runtime_ref, input_contract,
                output_contract, approval_policy, timeout_seconds, max_attempts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                registry._new_id("step"), workflow_id, step["step_key"], step["position"],
                step["name"], step["description"], step["executor_profile"], step["runtime_kind"],
                step["runtime_ref"], registry._json_dumps(step["input_contract"]),
                registry._json_dumps(step["output_contract"]), step["approval_policy"],
                step["timeout_seconds"], step["max_attempts"],
            ),
        )
    # This is a registry-only schedule projection.  It never calls Cron.
    conn.execute("DELETE FROM workflow_schedules WHERE workflow_id = ?", (workflow_id,))
    for schedule in metadata.get("schedules", []):
        if isinstance(schedule, dict) and schedule.get("cron_job_id"):
            conn.execute(
                "INSERT INTO workflow_schedules(workflow_id, profile, cron_job_id, enabled, last_verified_at) VALUES (?, ?, ?, ?, NULL)",
                (workflow_id, str(schedule.get("profile") or metadata["owner_profile"]),
                 str(schedule["cron_job_id"]), int(bool(schedule.get("enabled", True)))),
            )
    definition = registry.get_definition(conn, workflow_id).to_dict()
    definition["steps"] = [step.to_dict() for step in registry.list_steps(conn, workflow_id)]
    return definition


def restore_projection_transaction(
    conn, snapshot: dict[str, Any], *, candidate_workflow_id: str
) -> None:
    """Restore a snapshot captured by :func:`snapshot_projection` in a transaction."""
    definition = snapshot["definition"]
    if definition is None:
        conn.execute("DELETE FROM workflow_definitions WHERE id = ?", (candidate_workflow_id,))
        return
    workflow_id = str(definition["id"])
    conn.execute("DELETE FROM workflow_definitions WHERE id = ?", (workflow_id,))
    columns = list(definition)
    conn.execute(
        f"INSERT INTO workflow_definitions({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        [definition[column] for column in columns],
    )
    for table, rows in (("workflow_steps", snapshot["steps"]), ("workflow_schedules", snapshot["schedules"])):
        for row in rows:
            columns = list(row)
            conn.execute(
                f"INSERT INTO {table}({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [row[column] for column in columns],
            )
