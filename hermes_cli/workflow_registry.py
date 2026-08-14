"""SQLite persistence kernel for the Hermes Workflow Registry.

The registry is a shared machine-level catalog. Definitions and steps are
projections of canonical Markdown runbooks; run, step-run, and event tables are
the durable execution ledger.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from hermes_cli.sqlite_util import write_txn
from hermes_cli.workflow_models import (
    RUNTIME_KINDS,
    RUN_STATUSES,
    STEP_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    TERMINAL_STEP_STATUSES,
    WORKFLOW_STATUSES,
    WorkflowConflictError,
    WorkflowDefinition,
    WorkflowNotFoundError,
    WorkflowRun,
    WorkflowStateError,
    WorkflowStep,
    WorkflowStepRun,
)
from hermes_constants import get_default_hermes_root


SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workflow_definitions (
    id              TEXT PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    description     TEXT,
    owner_profile   TEXT NOT NULL,
    status          TEXT NOT NULL,
    runtime_kind    TEXT NOT NULL,
    runtime_ref     TEXT,
    source_path     TEXT,
    source_hash     TEXT,
    source_revision TEXT,
    kanban_board    TEXT,
    repair_task_id  TEXT,
    dedupe_strategy TEXT,
    timeout_seconds INTEGER,
    max_attempts    INTEGER,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    retired_at      INTEGER
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    id                TEXT PRIMARY KEY,
    workflow_id       TEXT NOT NULL REFERENCES workflow_definitions(id)
                          ON DELETE CASCADE,
    step_key          TEXT NOT NULL,
    position          INTEGER NOT NULL,
    name              TEXT NOT NULL,
    description       TEXT,
    executor_profile  TEXT,
    runtime_kind      TEXT,
    runtime_ref       TEXT,
    input_contract    TEXT,
    output_contract   TEXT,
    approval_policy   TEXT,
    timeout_seconds   INTEGER,
    max_attempts      INTEGER,
    UNIQUE(workflow_id, step_key),
    UNIQUE(workflow_id, position)
);

CREATE INDEX IF NOT EXISTS idx_workflow_steps_workflow_position
    ON workflow_steps(workflow_id, position);

CREATE TABLE IF NOT EXISTS workflow_schedules (
    workflow_id       TEXT NOT NULL REFERENCES workflow_definitions(id)
                          ON DELETE CASCADE,
    profile           TEXT NOT NULL,
    cron_job_id       TEXT NOT NULL,
    enabled           INTEGER NOT NULL DEFAULT 1,
    last_verified_at  INTEGER,
    PRIMARY KEY(workflow_id, profile, cron_job_id)
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id                TEXT PRIMARY KEY,
    workflow_id       TEXT NOT NULL REFERENCES workflow_definitions(id)
                          ON DELETE CASCADE,
    trigger_kind      TEXT NOT NULL,
    trigger_ref       TEXT,
    dedupe_key        TEXT,
    status            TEXT NOT NULL,
    current_step_key  TEXT,
    started_at        INTEGER NOT NULL,
    ended_at          INTEGER,
    summary           TEXT,
    error             TEXT,
    kanban_task_id    TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_runs_dedupe
    ON workflow_runs(workflow_id, dedupe_key)
    WHERE dedupe_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_started
    ON workflow_runs(workflow_id, started_at DESC);

CREATE TABLE IF NOT EXISTS workflow_step_runs (
    id                TEXT PRIMARY KEY,
    workflow_run_id   TEXT NOT NULL REFERENCES workflow_runs(id)
                          ON DELETE CASCADE,
    step_key          TEXT NOT NULL,
    attempt           INTEGER NOT NULL,
    status            TEXT NOT NULL,
    started_at        INTEGER,
    ended_at          INTEGER,
    summary           TEXT,
    error             TEXT,
    output_refs       TEXT,
    UNIQUE(workflow_run_id, step_key, attempt)
);

CREATE INDEX IF NOT EXISTS idx_workflow_step_runs_run
    ON workflow_step_runs(workflow_run_id, step_key, attempt);

CREATE TABLE IF NOT EXISTS workflow_events (
    id                TEXT PRIMARY KEY,
    entity_type       TEXT NOT NULL,
    entity_id         TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    created_at        INTEGER NOT NULL,
    payload           TEXT
);

CREATE INDEX IF NOT EXISTS idx_workflow_events_entity
    ON workflow_events(entity_type, entity_id, created_at);

CREATE TABLE IF NOT EXISTS runbook_activation_identities (
    approval_id   TEXT PRIMARY KEY,
    identity_json TEXT NOT NULL,
    workflow_id   TEXT NOT NULL,
    event_id      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_registry_meta (
    key               TEXT PRIMARY KEY,
    value             TEXT NOT NULL
);
"""


_INITIALIZED_PATHS: set[str] = set()


def workflow_registry_db_path() -> Path:
    """Return the shared machine-level workflow registry DB path."""
    return get_default_hermes_root() / "workflow_registry.db"


def _now() -> int:
    return int(time.time())


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _row_to_definition(row: sqlite3.Row) -> WorkflowDefinition:
    return WorkflowDefinition(**dict(row))


def _row_to_step(row: sqlite3.Row) -> WorkflowStep:
    data = dict(row)
    data["input_contract"] = _json_loads(data["input_contract"])
    data["output_contract"] = _json_loads(data["output_contract"])
    return WorkflowStep(**data)


def _row_to_run(row: sqlite3.Row) -> WorkflowRun:
    return WorkflowRun(**dict(row))


def _row_to_step_run(row: sqlite3.Row) -> WorkflowStepRun:
    data = dict(row)
    data["output_refs"] = _json_loads(data["output_refs"])
    return WorkflowStepRun(**data)


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open and initialize the shared workflow registry DB."""
    path = db_path if db_path is not None else workflow_registry_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(path.resolve())
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(conn, db_label="workflow_registry.db")
        conn.execute("PRAGMA foreign_keys=ON")
        if resolved not in _INITIALIZED_PATHS:
            init_db(conn)
            _INITIALIZED_PATHS.add(resolved)
    except Exception:
        conn.close()
        raise
    return conn


def connect_fd(db_fd: int, *, db_identity: str) -> sqlite3.Connection:
    """Open an already checked Registry inode without re-opening its leaf name.

    Activation holds ``db_fd`` from descriptor-anchored secure I/O. SQLite
    follows this process's FD link to that inode, so a later rename or symlink
    swap of ``workflow_registry.db`` cannot redirect identity/event writes.
    """
    if os.name != "posix" or db_fd < 0:
        raise OSError("descriptor-backed workflow registry access is unavailable")
    try:
        conn = sqlite3.connect(f"file:/proc/self/fd/{db_fd}?mode=rw", uri=True, timeout=30)
    except sqlite3.Error as exc:
        raise PermissionError("descriptor-backed workflow registry is unavailable or unsafe") from exc
    try:
        conn.row_factory = sqlite3.Row
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(conn, db_label="workflow_registry.db")
        conn.execute("PRAGMA foreign_keys=ON")
        if db_identity not in _INITIALIZED_PATHS:
            init_db(conn)
            _INITIALIZED_PATHS.add(db_identity)
    except Exception:
        conn.close()
        raise
    return conn


@contextlib.contextmanager
def connect_closing(db_path: Path | None = None):
    conn = connect(db_path=db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def connect_closing_fd(db_fd: int, *, db_identity: str):
    conn = connect_fd(db_fd, db_identity=db_identity)
    try:
        yield conn
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    """Initialize or migrate the registry schema."""
    with write_txn(conn):
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO workflow_registry_meta(key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )


def _validate_definition_fields(values: Mapping[str, Any]) -> None:
    status = values.get("status")
    runtime_kind = values.get("runtime_kind")
    if status not in WORKFLOW_STATUSES:
        raise ValueError(f"invalid workflow status: {status!r}")
    if runtime_kind not in RUNTIME_KINDS:
        raise ValueError(f"invalid workflow runtime_kind: {runtime_kind!r}")


def _event(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
) -> str:
    event_id = _new_id("evt")
    conn.execute(
        """
        INSERT INTO workflow_events(id, entity_type, entity_id, event_type, created_at, payload)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            entity_type,
            entity_id,
            event_type,
            _now(),
            _json_dumps(dict(payload or {})),
        ),
    )
    return event_id


def record_runbook_activation(
    conn: sqlite3.Connection,
    *,
    approval_id: str,
    identity: Mapping[str, Any],
    workflow_id: str,
    payload: Mapping[str, Any],
) -> tuple[bool, str]:
    """Record one activation identity inside the caller's write transaction.

    The database, rather than a preflight event scan, owns idempotency.  A
    duplicate is a replay only when every immutable approval binding matches.
    """
    identity_json = _json_dumps(dict(identity))
    row = conn.execute(
        "SELECT identity_json, workflow_id, event_id FROM runbook_activation_identities "
        "WHERE approval_id = ?",
        (approval_id,),
    ).fetchone()
    if row is not None:
        if row["identity_json"] != identity_json or row["workflow_id"] != workflow_id:
            raise WorkflowConflictError("approval id is already bound to a different activation")
        return True, str(row["event_id"])
    event_id = _event(
        conn,
        "workflow_definition",
        workflow_id,
        "runbook_proposal_activated",
        dict(payload),
    )
    conn.execute(
        "INSERT INTO runbook_activation_identities(approval_id, identity_json, workflow_id, event_id) "
        "VALUES (?, ?, ?, ?)",
        (approval_id, identity_json, workflow_id, event_id),
    )
    return False, event_id


def record_event(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
) -> None:
    """Append a durable registry event after validating its target exists."""
    if entity_type == "workflow_definition":
        get_definition(conn, entity_id)
    with write_txn(conn):
        _event(conn, entity_type, entity_id, event_type, payload)


def create_definition(conn: sqlite3.Connection, **values: Any) -> WorkflowDefinition:
    """Create a workflow definition projection."""
    workflow_id = str(values.get("id") or _new_id("wf"))
    now = _now()
    record = {
        "id": workflow_id,
        "slug": values["slug"],
        "name": values["name"],
        "description": values.get("description"),
        "owner_profile": values["owner_profile"],
        "status": values.get("status", "draft"),
        "runtime_kind": values.get("runtime_kind", "hermes"),
        "runtime_ref": values.get("runtime_ref"),
        "source_path": values.get("source_path"),
        "source_hash": values.get("source_hash"),
        "source_revision": values.get("source_revision"),
        "kanban_board": values.get("kanban_board"),
        "repair_task_id": values.get("repair_task_id"),
        "dedupe_strategy": values.get("dedupe_strategy"),
        "timeout_seconds": values.get("timeout_seconds"),
        "max_attempts": values.get("max_attempts"),
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "retired_at": None,
    }
    _validate_definition_fields(record)
    columns = list(record)
    with write_txn(conn):
        try:
            conn.execute(
                f"INSERT INTO workflow_definitions({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [record[column] for column in columns],
            )
        except sqlite3.IntegrityError as exc:
            raise WorkflowConflictError(str(exc)) from exc
        _event(conn, "workflow_definition", workflow_id, "created", record)
    return get_definition(conn, workflow_id)


def get_definition(conn: sqlite3.Connection, workflow_id: str) -> WorkflowDefinition:
    row = conn.execute(
        "SELECT * FROM workflow_definitions WHERE id = ?", (workflow_id,)
    ).fetchone()
    if row is None:
        raise WorkflowNotFoundError(f"workflow definition not found: {workflow_id}")
    return _row_to_definition(row)


def get_definition_by_slug(conn: sqlite3.Connection, slug: str) -> WorkflowDefinition:
    row = conn.execute(
        "SELECT * FROM workflow_definitions WHERE slug = ?", (slug,)
    ).fetchone()
    if row is None:
        raise WorkflowNotFoundError(f"workflow definition not found: {slug}")
    return _row_to_definition(row)


def list_definitions(conn: sqlite3.Connection) -> list[WorkflowDefinition]:
    rows = conn.execute(
        "SELECT * FROM workflow_definitions ORDER BY status, slug"
    ).fetchall()
    return [_row_to_definition(row) for row in rows]


def update_definition(
    conn: sqlite3.Connection,
    workflow_id: str,
    *,
    expected_version: int,
    **changes: Any,
) -> WorkflowDefinition:
    """Update a workflow definition with optimistic concurrency."""
    if not changes:
        return get_definition(conn, workflow_id)
    allowed = {
        "slug",
        "name",
        "description",
        "owner_profile",
        "status",
        "runtime_kind",
        "runtime_ref",
        "source_path",
        "source_hash",
        "source_revision",
        "kanban_board",
        "repair_task_id",
        "dedupe_strategy",
        "timeout_seconds",
        "max_attempts",
        "retired_at",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unsupported workflow fields: {sorted(unknown)}")
    current = get_definition(conn, workflow_id)
    candidate = current.to_dict()
    candidate.update(changes)
    _validate_definition_fields(candidate)
    candidate["updated_at"] = _now()
    assignments = [f"{key} = ?" for key in changes]
    assignments.extend(["updated_at = ?", "version = version + 1"])
    params = [changes[key] for key in changes]
    params.extend([candidate["updated_at"], workflow_id, expected_version])
    with write_txn(conn):
        cur = conn.execute(
            f"UPDATE workflow_definitions SET {', '.join(assignments)} "
            "WHERE id = ? AND version = ?",
            params,
        )
        if cur.rowcount != 1:
            raise WorkflowConflictError(
                f"workflow definition changed concurrently: {workflow_id}"
            )
        _event(conn, "workflow_definition", workflow_id, "updated", changes)
    return get_definition(conn, workflow_id)


def retire_definition(
    conn: sqlite3.Connection,
    workflow_id: str,
    *,
    expected_version: int,
) -> WorkflowDefinition:
    return update_definition(
        conn,
        workflow_id,
        expected_version=expected_version,
        status="retired",
        retired_at=_now(),
    )


def replace_steps(
    conn: sqlite3.Connection,
    workflow_id: str,
    steps: Iterable[Mapping[str, Any]],
) -> list[WorkflowStep]:
    """Replace projected steps for a workflow atomically."""
    get_definition(conn, workflow_id)
    normalized: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_positions: set[int] = set()
    for index, step in enumerate(steps):
        step_key = str(step["step_key"])
        position = int(step.get("position", index))
        if step_key in seen_keys or position in seen_positions:
            raise WorkflowConflictError("duplicate step_key or position")
        seen_keys.add(step_key)
        seen_positions.add(position)
        runtime_kind = step.get("runtime_kind")
        if runtime_kind is not None and runtime_kind not in RUNTIME_KINDS:
            raise ValueError(f"invalid step runtime_kind: {runtime_kind!r}")
        normalized.append(
            {
                "id": str(step.get("id") or _new_id("step")),
                "workflow_id": workflow_id,
                "step_key": step_key,
                "position": position,
                "name": step["name"],
                "description": step.get("description"),
                "executor_profile": step.get("executor_profile"),
                "runtime_kind": runtime_kind,
                "runtime_ref": step.get("runtime_ref"),
                "input_contract": _json_dumps(step.get("input_contract")),
                "output_contract": _json_dumps(step.get("output_contract")),
                "approval_policy": step.get("approval_policy"),
                "timeout_seconds": step.get("timeout_seconds"),
                "max_attempts": step.get("max_attempts"),
            }
        )
    columns = [
        "id",
        "workflow_id",
        "step_key",
        "position",
        "name",
        "description",
        "executor_profile",
        "runtime_kind",
        "runtime_ref",
        "input_contract",
        "output_contract",
        "approval_policy",
        "timeout_seconds",
        "max_attempts",
    ]
    with write_txn(conn):
        conn.execute("DELETE FROM workflow_steps WHERE workflow_id = ?", (workflow_id,))
        for row in normalized:
            conn.execute(
                f"INSERT INTO workflow_steps({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [row[column] for column in columns],
            )
        _event(
            conn,
            "workflow_definition",
            workflow_id,
            "steps_replaced",
            {"step_count": len(normalized)},
        )
    return list_steps(conn, workflow_id)


def list_steps(conn: sqlite3.Connection, workflow_id: str) -> list[WorkflowStep]:
    rows = conn.execute(
        "SELECT * FROM workflow_steps WHERE workflow_id = ? ORDER BY position",
        (workflow_id,),
    ).fetchall()
    return [_row_to_step(row) for row in rows]


def link_schedule(
    conn: sqlite3.Connection,
    workflow_id: str,
    *,
    profile: str,
    cron_job_id: str,
    enabled: bool = True,
    last_verified_at: int | None = None,
) -> None:
    get_definition(conn, workflow_id)
    with write_txn(conn):
        conn.execute(
            """
            INSERT INTO workflow_schedules(
                workflow_id, profile, cron_job_id, enabled, last_verified_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(workflow_id, profile, cron_job_id)
            DO UPDATE SET enabled=excluded.enabled,
                          last_verified_at=excluded.last_verified_at
            """,
            (workflow_id, profile, cron_job_id, int(enabled), last_verified_at),
        )
        _event(
            conn,
            "workflow_definition",
            workflow_id,
            "schedule_linked",
            {"profile": profile, "cron_job_id": cron_job_id, "enabled": enabled},
        )


def prune_missing_schedule_links(
    conn: sqlite3.Connection,
    live_links: set[tuple[str, str]],
) -> list[dict[str, str]]:
    """Remove schedule projections whose profile/job pair no longer exists."""
    rows = conn.execute(
        "SELECT workflow_id, profile, cron_job_id FROM workflow_schedules"
    ).fetchall()
    stale = [
        dict(row)
        for row in rows
        if (str(row["profile"]), str(row["cron_job_id"])) not in live_links
    ]
    if not stale:
        return []
    with write_txn(conn):
        for row in stale:
            conn.execute(
                "DELETE FROM workflow_schedules "
                "WHERE workflow_id = ? AND profile = ? AND cron_job_id = ?",
                (row["workflow_id"], row["profile"], row["cron_job_id"]),
            )
            _event(
                conn,
                "workflow_definition",
                row["workflow_id"],
                "stale_schedule_unlinked",
                {"profile": row["profile"], "cron_job_id": row["cron_job_id"]},
            )
    return stale


def start_run(
    conn: sqlite3.Connection,
    workflow_id: str,
    *,
    trigger_kind: str,
    trigger_ref: str | None = None,
    dedupe_key: str | None = None,
    kanban_task_id: str | None = None,
    reuse_existing: bool = True,
) -> WorkflowRun:
    get_definition(conn, workflow_id)
    run_id = _new_id("run")
    now = _now()
    with write_txn(conn):
        try:
            conn.execute(
                """
                INSERT INTO workflow_runs(
                    id, workflow_id, trigger_kind, trigger_ref, dedupe_key,
                    status, current_step_key, started_at, kanban_task_id
                )
                VALUES (?, ?, ?, ?, ?, 'running', NULL, ?, ?)
                """,
                (
                    run_id,
                    workflow_id,
                    trigger_kind,
                    trigger_ref,
                    dedupe_key,
                    now,
                    kanban_task_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if reuse_existing and dedupe_key is not None:
                row = conn.execute(
                    """
                    SELECT * FROM workflow_runs
                    WHERE workflow_id = ? AND dedupe_key = ?
                    """,
                    (workflow_id, dedupe_key),
                ).fetchone()
                if row is not None:
                    return _row_to_run(row)
            raise WorkflowConflictError(str(exc)) from exc
        _event(
            conn,
            "workflow_run",
            run_id,
            "started",
            {
                "workflow_id": workflow_id,
                "trigger_kind": trigger_kind,
                "trigger_ref": trigger_ref,
                "dedupe_key": dedupe_key,
            },
        )
    return get_run(conn, run_id)


def get_run(conn: sqlite3.Connection, run_id: str) -> WorkflowRun:
    row = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise WorkflowNotFoundError(f"workflow run not found: {run_id}")
    return _row_to_run(row)


def list_runs(conn: sqlite3.Connection, workflow_id: str) -> list[WorkflowRun]:
    rows = conn.execute(
        "SELECT * FROM workflow_runs WHERE workflow_id = ? ORDER BY started_at DESC",
        (workflow_id,),
    ).fetchall()
    return [_row_to_run(row) for row in rows]


def _next_attempt(conn: sqlite3.Connection, run_id: str, step_key: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(attempt), 0) + 1 AS attempt
        FROM workflow_step_runs
        WHERE workflow_run_id = ? AND step_key = ?
        """,
        (run_id, step_key),
    ).fetchone()
    return int(row["attempt"])


def start_step(
    conn: sqlite3.Connection,
    run_id: str,
    step_key: str,
    *,
    attempt: int | None = None,
) -> WorkflowStepRun:
    run = get_run(conn, run_id)
    if run.status in TERMINAL_RUN_STATUSES:
        raise WorkflowStateError(f"cannot start a step for terminal run {run_id}")
    attempt_value = attempt
    step_run_id = _new_id("step_run")
    now = _now()
    with write_txn(conn):
        if attempt_value is None:
            attempt_value = _next_attempt(conn, run_id, step_key)
        conn.execute(
            """
            INSERT INTO workflow_step_runs(
                id, workflow_run_id, step_key, attempt, status, started_at
            )
            VALUES (?, ?, ?, ?, 'running', ?)
            """,
            (step_run_id, run_id, step_key, attempt_value, now),
        )
        conn.execute(
            """
            UPDATE workflow_runs
            SET status = 'running', current_step_key = ?
            WHERE id = ?
            """,
            (step_key, run_id),
        )
        _event(
            conn,
            "workflow_step_run",
            step_run_id,
            "started",
            {"run_id": run_id, "step_key": step_key, "attempt": attempt_value},
        )
    return get_step_run(conn, step_run_id)


def get_step_run(conn: sqlite3.Connection, step_run_id: str) -> WorkflowStepRun:
    row = conn.execute(
        "SELECT * FROM workflow_step_runs WHERE id = ?", (step_run_id,)
    ).fetchone()
    if row is None:
        raise WorkflowNotFoundError(f"workflow step run not found: {step_run_id}")
    return _row_to_step_run(row)


def latest_step_run(
    conn: sqlite3.Connection, run_id: str, step_key: str
) -> WorkflowStepRun:
    row = conn.execute(
        """
        SELECT * FROM workflow_step_runs
        WHERE workflow_run_id = ? AND step_key = ?
        ORDER BY attempt DESC
        LIMIT 1
        """,
        (run_id, step_key),
    ).fetchone()
    if row is None:
        raise WorkflowNotFoundError(f"workflow step run not found: {run_id}/{step_key}")
    return _row_to_step_run(row)


def finish_step(
    conn: sqlite3.Connection,
    step_run_id: str,
    *,
    status: str,
    summary: str | None = None,
    error: str | None = None,
    output_refs: Mapping[str, Any] | None = None,
) -> WorkflowStepRun:
    if status not in STEP_RUN_STATUSES:
        raise ValueError(f"invalid step-run status: {status!r}")
    if status not in TERMINAL_STEP_STATUSES and status != "waiting_for_approval":
        raise WorkflowStateError(f"finish_step requires terminal/waiting status: {status}")
    current = get_step_run(conn, step_run_id)
    if current.status in TERMINAL_STEP_STATUSES:
        raise WorkflowStateError(f"step run already terminal: {step_run_id}")
    now = _now()
    with write_txn(conn):
        conn.execute(
            """
            UPDATE workflow_step_runs
            SET status = ?, ended_at = ?, summary = ?, error = ?, output_refs = ?
            WHERE id = ?
            """,
            (status, now, summary, error, _json_dumps(output_refs), step_run_id),
        )
        _event(
            conn,
            "workflow_step_run",
            step_run_id,
            status,
            {"summary": summary, "error": error, "output_refs": output_refs},
        )
    return get_step_run(conn, step_run_id)


def complete_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str = "succeeded",
    summary: str | None = None,
    error: str | None = None,
) -> WorkflowRun:
    if status not in TERMINAL_RUN_STATUSES:
        raise ValueError(f"run completion status must be terminal: {status!r}")
    current = get_run(conn, run_id)
    if current.status in TERMINAL_RUN_STATUSES:
        raise WorkflowStateError(f"workflow run already terminal: {run_id}")
    with write_txn(conn):
        conn.execute(
            """
            UPDATE workflow_runs
            SET status = ?, ended_at = ?, summary = ?, error = ?
            WHERE id = ?
            """,
            (status, _now(), summary, error, run_id),
        )
        _event(
            conn,
            "workflow_run",
            run_id,
            status,
            {"summary": summary, "error": error},
        )
    return get_run(conn, run_id)


def list_events(
    conn: sqlite3.Connection,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM workflow_events"
    params: list[Any] = []
    filters: list[str] = []
    if entity_type is not None:
        filters.append("entity_type = ?")
        params.append(entity_type)
    if entity_id is not None:
        filters.append("entity_id = ?")
        params.append(entity_id)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY created_at, id"
    rows = conn.execute(query, params).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        data["payload"] = _json_loads(data["payload"])
        result.append(data)
    return result


def export_json(conn: sqlite3.Connection) -> dict[str, Any]:
    """Export registry state to a JSON-serializable dictionary."""
    tables = [
        "workflow_definitions",
        "workflow_steps",
        "workflow_schedules",
        "workflow_runs",
        "workflow_step_runs",
        "workflow_events",
        "workflow_registry_meta",
    ]
    exported: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "tables": {}}
    for table in tables:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        exported["tables"][table] = [dict(row) for row in rows]
    return exported


def backup_db(conn: sqlite3.Connection, destination: Path) -> Path:
    """Create a SQLite backup at ``destination``."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(destination)) as backup_conn:
        conn.backup(backup_conn)
    return destination


def restore_backup(source: Path, destination: Path) -> Path:
    """Restore a previously backed-up registry DB file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, tmp)
    tmp.replace(destination)
    return destination
