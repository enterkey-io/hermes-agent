"""Runbooks dashboard plugin backend.

Mounted at /api/plugins/runbooks/ by the dashboard plugin loader.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from hermes_cli import runbook_store
from hermes_cli import workflow_registry as registry
from hermes_cli.runbook_schema import RunbookValidationError, split_frontmatter
from hermes_cli.workflow_models import (
    WorkflowConflictError,
    WorkflowNotFoundError,
    WorkflowStateError,
)


router = APIRouter()


class RunbookSaveRequest(BaseModel):
    markdown: str = Field(..., min_length=1)
    approved_by: str = Field(..., min_length=1)


class RunbookProposalRequest(BaseModel):
    markdown: str = Field(..., min_length=1)
    proposed_by: str = Field(..., min_length=1)
    summary: str | None = None


class MarkdownRequest(BaseModel):
    markdown: str = Field(..., min_length=1)


class WorkflowCreateRequest(BaseModel):
    slug: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    owner_profile: str = Field(..., min_length=1)
    description: str | None = None
    status: str = "draft"
    runtime_kind: str = "hermes"
    runtime_ref: str | None = None
    source_path: str | None = None
    source_hash: str | None = None
    source_revision: str | None = None
    kanban_board: str | None = None
    repair_task_id: str | None = None
    dedupe_strategy: str | None = None
    timeout_seconds: int | None = None
    max_attempts: int | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowPatchRequest(BaseModel):
    expected_version: int
    changes: dict[str, Any] = Field(default_factory=dict)
    steps: list[dict[str, Any]] | None = None


class RunStartRequest(BaseModel):
    workflow_id: str | None = None
    workflow_slug: str | None = None
    trigger_kind: str = "manual"
    trigger_ref: str | None = None
    dedupe_key: str | None = None
    kanban_task_id: str | None = None
    reuse_existing: bool = True


class StepStartRequest(BaseModel):
    step_key: str = Field(..., min_length=1)
    attempt: int | None = None


class StepFinishRequest(BaseModel):
    status: str
    summary: str | None = None
    error: str | None = None
    output_refs: dict[str, Any] | None = None


class RunCompleteRequest(BaseModel):
    status: str = "succeeded"
    summary: str | None = None
    error: str | None = None


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, WorkflowNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (WorkflowConflictError, WorkflowStateError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (RunbookValidationError, ValueError, PermissionError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _definition_dict(conn, workflow_id: str) -> dict[str, Any]:
    definition = registry.get_definition(conn, workflow_id).to_dict()
    definition["steps"] = [step.to_dict() for step in registry.list_steps(conn, workflow_id)]
    definition["runs"] = _runs_for_workflow(conn, workflow_id, limit=25)
    definition["events"] = registry.list_events(
        conn,
        entity_type="workflow_definition",
        entity_id=workflow_id,
    )
    return definition


def _runs_for_workflow(conn, workflow_id: str, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM workflow_runs
        WHERE workflow_id = ?
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (workflow_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _list_runs(conn, *, workflow_id: str | None, limit: int) -> list[dict[str, Any]]:
    if workflow_id:
        rows = conn.execute(
            """
            SELECT * FROM workflow_runs
            WHERE workflow_id = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (workflow_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM workflow_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["steps"] = _step_runs_for_run(conn, item["id"])
        result.append(item)
    return result


def _step_runs_for_run(conn, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM workflow_step_runs
        WHERE workflow_run_id = ?
        ORDER BY step_key, attempt
        """,
        (run_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("output_refs"):
            item["output_refs"] = json.loads(item["output_refs"])
        result.append(item)
    return result


def _metadata_steps_to_registry_steps(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for index, step in enumerate(metadata["steps"]):
        steps.append(
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
    return steps


def _sync_runbook_projection(record: runbook_store.RunbookRecord) -> dict[str, Any]:
    parsed = runbook_store.read_runbook(Path(record.path))
    metadata = parsed.metadata
    runtime = metadata["runtime"]
    timeout = metadata.get("timeout")
    retry = metadata.get("retry")
    dedupe = metadata.get("deduplication")
    definition_values = {
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
                for key, value in definition_values.items()
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
                definition = registry.create_definition(conn, **definition_values)
            except WorkflowConflictError:
                existing = registry.get_definition_by_slug(conn, metadata["slug"])
                definition = registry.update_definition(
                    conn,
                    existing.id,
                    expected_version=existing.version,
                    **{
                        key: value
                        for key, value in definition_values.items()
                        if key != "id"
                    },
                )
        registry.replace_steps(
            conn,
            definition.id,
            _metadata_steps_to_registry_steps(metadata),
        )
        for schedule in metadata.get("schedules", []):
            if not isinstance(schedule, dict):
                continue
            profile = str(schedule.get("profile") or metadata["owner_profile"])
            cron_job_id = schedule.get("cron_job_id") or schedule.get("id")
            if cron_job_id:
                registry.link_schedule(
                    conn,
                    definition.id,
                    profile=profile,
                    cron_job_id=str(cron_job_id),
                    enabled=bool(schedule.get("enabled", True)),
                )
        from hermes_cli.workflow_runtime import sync_runbook_cron_jobs

        sync_runbook_cron_jobs(metadata["slug"])
        return _definition_dict(conn, definition.id)


def _resolve_workflow_id(conn, workflow_id: str | None, workflow_slug: str | None) -> str:
    if workflow_id:
        return workflow_id
    if workflow_slug:
        return registry.get_definition_by_slug(conn, workflow_slug).id
    raise ValueError("workflow_id or workflow_slug is required")


def _proposal_records(slug: str) -> list[dict[str, Any]]:
    proposals = runbook_store.runbook_path(slug).parent / ".proposals"
    if not proposals.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(proposals.glob("*.json"), reverse=True):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return records


@router.get("/overview")
async def overview() -> dict[str, Any]:
    with registry.connect_closing() as conn:
        definitions = [item.to_dict() for item in registry.list_definitions(conn)]
        runs = _list_runs(conn, workflow_id=None, limit=50)
    runbooks = [record.to_dict() for record in runbook_store.list_runbooks()]
    return {
        "counts": {
            "runbooks": len(runbooks),
            "workflows": len(definitions),
            "active_workflows": len([w for w in definitions if w["status"] == "active"]),
            "recent_runs": len(runs),
        },
        "runbooks": runbooks,
        "workflows": definitions,
        "recent_runs": runs,
    }


@router.get("/runbooks")
async def list_runbooks(q: str = "") -> dict[str, Any]:
    records = runbook_store.search_runbooks(q) if q else runbook_store.list_runbooks()
    return {"runbooks": [record.to_dict() for record in records]}


@router.get("/runbooks/{slug}")
async def get_runbook(slug: str) -> dict[str, Any]:
    path = runbook_store.runbook_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"runbook not found: {slug}")
    try:
        markdown = path.read_text(encoding="utf-8")
        parsed = split_frontmatter(markdown)
    except Exception as exc:
        raise _http_error(exc)
    record = next(
        (
            item
            for item in runbook_store.list_runbooks()
            if item.slug == parsed.metadata["slug"]
        ),
        None,
    )
    if record is None:
        record = runbook_store.RunbookRecord(
            id=parsed.metadata["id"],
            slug=parsed.metadata["slug"],
            title=parsed.metadata["title"],
            purpose=parsed.metadata["purpose"],
            owner_profile=parsed.metadata["owner_profile"],
            status=parsed.metadata["status"],
            path=str(path),
            source_hash="",
            revision=str(parsed.metadata.get("source_revision") or ""),
        )
    revisions = [
        {"path": str(revision), "name": revision.name}
        for revision in runbook_store.list_revisions(slug)
    ]
    return {
        "record": record.to_dict(),
        "metadata": parsed.metadata,
        "body": parsed.body,
        "markdown": markdown,
        "revisions": revisions,
        "proposals": _proposal_records(slug),
    }


@router.put("/runbooks/{slug}")
async def save_runbook(slug: str, request: RunbookSaveRequest) -> dict[str, Any]:
    try:
        parsed = split_frontmatter(request.markdown)
        if parsed.metadata["slug"] != slug:
            raise ValueError("runbook slug does not match URL")
        record = runbook_store.save_runbook(
            parsed.metadata,
            parsed.body,
            approved_by=request.approved_by,
        )
        workflow = _sync_runbook_projection(record)
    except Exception as exc:
        raise _http_error(exc)
    return {"runbook": record.to_dict(), "workflow": workflow}


@router.post("/runbooks/{slug}/proposals")
async def propose_runbook_edit(
    slug: str,
    request: RunbookProposalRequest,
) -> dict[str, Any]:
    try:
        parsed = split_frontmatter(request.markdown)
        if parsed.metadata["slug"] != slug:
            raise ValueError("runbook slug does not match URL")
        path = runbook_store.propose_edit(
            slug,
            request.markdown,
            proposed_by=request.proposed_by,
            summary=request.summary,
        )
    except Exception as exc:
        raise _http_error(exc)
    return {"proposal": {"path": str(path), "name": path.name}}


@router.post("/runbooks/preview")
async def preview_runbook(request: MarkdownRequest) -> dict[str, str]:
    try:
        html = runbook_store.render_preview(request.markdown)
    except Exception as exc:
        raise _http_error(exc)
    return {"html": html}


@router.post("/runbooks/{slug}/diff")
async def diff_runbook(slug: str, request: MarkdownRequest) -> dict[str, str]:
    try:
        diff = runbook_store.diff_against_current(slug, request.markdown)
    except Exception as exc:
        raise _http_error(exc)
    return {"diff": diff}


@router.get("/workflows")
async def list_workflows() -> dict[str, Any]:
    with registry.connect_closing() as conn:
        workflows = [item.to_dict() for item in registry.list_definitions(conn)]
    return {"workflows": workflows}


@router.post("/workflows")
async def create_workflow(request: WorkflowCreateRequest) -> dict[str, Any]:
    try:
        with registry.connect_closing() as conn:
            workflow = registry.create_definition(conn, **request.dict(exclude={"steps"}))
            if request.steps:
                registry.replace_steps(conn, workflow.id, request.steps)
            return {"workflow": _definition_dict(conn, workflow.id)}
    except Exception as exc:
        raise _http_error(exc)


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str) -> dict[str, Any]:
    try:
        with registry.connect_closing() as conn:
            return {"workflow": _definition_dict(conn, workflow_id)}
    except Exception as exc:
        raise _http_error(exc)


@router.patch("/workflows/{workflow_id}")
async def patch_workflow(
    workflow_id: str,
    request: WorkflowPatchRequest,
) -> dict[str, Any]:
    try:
        with registry.connect_closing() as conn:
            registry.update_definition(
                conn,
                workflow_id,
                expected_version=request.expected_version,
                **request.changes,
            )
            if request.steps is not None:
                registry.replace_steps(conn, workflow_id, request.steps)
            return {"workflow": _definition_dict(conn, workflow_id)}
    except Exception as exc:
        raise _http_error(exc)


@router.get("/runs")
async def list_runs(
    workflow_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    with registry.connect_closing() as conn:
        return {"runs": _list_runs(conn, workflow_id=workflow_id, limit=limit)}


@router.post("/runs")
async def start_run(request: RunStartRequest) -> dict[str, Any]:
    try:
        with registry.connect_closing() as conn:
            workflow_id = _resolve_workflow_id(
                conn,
                request.workflow_id,
                request.workflow_slug,
            )
            run = registry.start_run(
                conn,
                workflow_id,
                trigger_kind=request.trigger_kind,
                trigger_ref=request.trigger_ref,
                dedupe_key=request.dedupe_key,
                kanban_task_id=request.kanban_task_id,
                reuse_existing=request.reuse_existing,
            )
            return {"run": run.to_dict()}
    except Exception as exc:
        raise _http_error(exc)


@router.post("/runs/{run_id}/steps")
async def start_step(run_id: str, request: StepStartRequest) -> dict[str, Any]:
    try:
        with registry.connect_closing() as conn:
            step = registry.start_step(
                conn,
                run_id,
                request.step_key,
                attempt=request.attempt,
            )
            return {"step_run": step.to_dict()}
    except Exception as exc:
        raise _http_error(exc)


@router.post("/step-runs/{step_run_id}/finish")
async def finish_step(
    step_run_id: str,
    request: StepFinishRequest,
) -> dict[str, Any]:
    try:
        with registry.connect_closing() as conn:
            step = registry.finish_step(
                conn,
                step_run_id,
                status=request.status,
                summary=request.summary,
                error=request.error,
                output_refs=request.output_refs,
            )
            return {"step_run": step.to_dict()}
    except Exception as exc:
        raise _http_error(exc)


@router.post("/runs/{run_id}/complete")
async def complete_run(run_id: str, request: RunCompleteRequest) -> dict[str, Any]:
    try:
        with registry.connect_closing() as conn:
            run = registry.complete_run(
                conn,
                run_id,
                status=request.status,
                summary=request.summary,
                error=request.error,
            )
            return {"run": run.to_dict()}
    except Exception as exc:
        raise _http_error(exc)


@router.get("/events")
async def list_events(
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    try:
        with registry.connect_closing() as conn:
            return {
                "events": registry.list_events(
                    conn,
                    entity_type=entity_type,
                    entity_id=entity_id,
                )
            }
    except sqlite3.Error as exc:
        raise _http_error(exc)
