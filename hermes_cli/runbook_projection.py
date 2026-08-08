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
