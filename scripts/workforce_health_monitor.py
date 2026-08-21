#!/usr/bin/env python3
"""Deterministically turn recurring Hermes Cron failures into owned repair work.

The monitor spends no model tokens while healthy. A failure must recur at least
twice before it is attached to an existing active task or creates one canonical
Aurora triage card. State prevents repeat comments and card fanout. Two later
successful executions close only cards created by this monitor; pre-existing
repair work receives recovery evidence but keeps its own acceptance lifecycle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any

from agent.redact import redact_sensitive_text
from hermes_cli import kanban_db
from hermes_cli.workforce_org import load_organization


FAILURE_STATUSES = {"error", "failed", "unknown"}
ACTIVE_TASK_STATUSES = {"triage", "todo", "ready", "running", "blocked", "review", "scheduled"}


def _safe_error(value: Any) -> str:
    return redact_sensitive_text(str(value or "unspecified failure"), force=True)[:800]


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _jobs(profile: Path) -> list[dict[str, Any]]:
    raw = _read_json(profile / "cron" / "jobs.json", {})
    rows = raw.get("jobs", []) if isinstance(raw, dict) else raw
    return [row for row in rows if isinstance(row, dict)]


def _two_recent_successes(profile: Path, job_id: str) -> bool:
    database = profile / "cron" / "executions.db"
    if not database.is_file():
        return False
    conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT status FROM executions WHERE job_id=? "
            "ORDER BY claimed_at DESC, id DESC LIMIT 2",
            (job_id,),
        ).fetchall()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return len(rows) == 2 and all(str(row[0]) == "completed" for row in rows)


def _active_matching_task(conn: sqlite3.Connection, job_id: str, job_name: str):
    name_probe = " ".join(job_name.split())[:80]
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status IN ("
        + ",".join("?" for _ in ACTIVE_TASK_STATUSES)
        + ") AND (body LIKE ? OR title LIKE ? OR body LIKE ?) "
        "ORDER BY created_at DESC LIMIT 1",
        (*sorted(ACTIVE_TASK_STATUSES), f"%{job_id}%", f"%{name_probe}%", f"%{name_probe}%"),
    ).fetchone()
    return rows


def _record_failure(
    conn: sqlite3.Connection,
    *,
    profile_name: str,
    job: dict[str, Any],
    state_row: dict[str, Any],
) -> tuple[str, bool]:
    job_id = str(job.get("id") or "")
    job_name = str(job.get("name") or job_id)
    existing = _active_matching_task(conn, job_id, job_name)
    if existing:
        task_id = str(existing["id"])
        created = False
    else:
        episode = int(state_row.get("episode", 0)) + 1
        error = _safe_error(job.get("last_error"))
        body = json.dumps(
            {
                "kind": "recurring_workflow_failure",
                "profile": profile_name,
                "job_id": job_id,
                "job_name": job_name,
                "failure_streak": int(job.get("failure_streak") or 0),
                "last_status": str(job.get("last_status") or ""),
                "last_run_at": str(job.get("last_run_at") or ""),
                "sanitized_error": error,
                "decision_owner": "aurora",
                "required_outcome": "Route the defect to the canonical technical owner, repair it, and verify two consecutive successful executions.",
                "reporting": "Internal control-plane incident. Do not tell Elliott he is the blocker and do not deliver raw failure chatter to an Elliott-visible room.",
            },
            indent=2,
            sort_keys=True,
        )
        task_id = kanban_db.create_task(
            conn,
            title=f"Repair recurring Cron failure: {profile_name} / {job_name}"[:180],
            body=body,
            assignee="aurora",
            created_by="workforce-health-monitor",
            workspace_kind="scratch",
            initial_status="running",
            idempotency_key=f"workforce-health:cron:{profile_name}:{job_id}:episode:{episode}",
            max_runtime_seconds=900,
        )
        state_row["episode"] = episode
        created = True
    kanban_db.add_comment(
        conn,
        task_id,
        "workforce-health-monitor",
        "Recurring failure detected deterministically: "
        f"profile={profile_name}; job={job_name} ({job_id}); "
        f"failure_streak={int(job.get('failure_streak') or 0)}; "
        f"last_status={job.get('last_status')}; last_run_at={job.get('last_run_at')}; "
        f"sanitized_error={_safe_error(job.get('last_error'))}",
    )
    return task_id, created


def run(*, organization: Path, database: Path, state_path: Path) -> dict[str, Any]:
    org = load_organization(organization, validate_profiles=True)
    state = _read_json(state_path, {"schema_version": 1, "findings": {}})
    findings = state.setdefault("findings", {})
    now = int(time.time())
    detected = attached = created = recovered = 0

    with kanban_db.connect_closing(database) as conn:
        for agent in org.operational_agents(include_planned=False):
            profile = Path(str(agent.profile_path))
            for job in _jobs(profile):
                job_id = str(job.get("id") or "").strip()
                if not job_id:
                    continue
                key = f"cron:{profile.name}:{job_id}"
                row = findings.setdefault(key, {"episode": 0, "status": "healthy"})
                is_failure = (
                    str(job.get("last_status") or "").lower() in FAILURE_STATUSES
                    and int(job.get("failure_streak") or 0) >= 2
                )
                signature = hashlib.sha256(
                    (str(job.get("last_status")) + "\0" + _safe_error(job.get("last_error"))).encode()
                ).hexdigest()
                if is_failure:
                    detected += 1
                    if row.get("status") != "active" or row.get("signature") != signature:
                        task_id, was_created = _record_failure(
                            conn,
                            profile_name=profile.name,
                            job=job,
                            state_row=row,
                        )
                        row.update({
                            "status": "active",
                            "task_id": task_id,
                            "monitor_created": was_created,
                            "signature": signature,
                            "first_seen_at": row.get("first_seen_at") or now,
                            "last_seen_at": now,
                        })
                        created += int(was_created)
                        attached += int(not was_created)
                    else:
                        row["last_seen_at"] = now
                    continue

                if row.get("status") == "active" and _two_recent_successes(profile, job_id):
                    task_id = str(row.get("task_id") or "")
                    task = kanban_db.get_task(conn, task_id) if task_id else None
                    if task:
                        kanban_db.add_comment(
                            conn,
                            task_id,
                            "workforce-health-monitor",
                            "Deterministic recovery evidence: the two most recent scheduler executions completed successfully. This proves scheduler-path recovery only; linked business-outcome acceptance remains with its accountable owner.",
                        )
                        if row.get("monitor_created") and task.status in ACTIVE_TASK_STATUSES:
                            kanban_db.complete_task(
                                conn,
                                task_id,
                                result="Recurring scheduler failure cleared after two consecutive completed executions.",
                                summary="Recurring scheduler failure cleared; two consecutive executions completed.",
                            )
                    row.update({"status": "recovered", "recovered_at": now})
                    recovered += 1

    state["updated_at"] = now
    _write_state(state_path, state)
    return {
        "detected": detected,
        "created": created,
        "attached": attached,
        "recovered": recovered,
        "state": str(state_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run(
        organization=args.organization,
        database=args.database,
        state_path=args.state,
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
