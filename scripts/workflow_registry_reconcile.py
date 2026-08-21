#!/usr/bin/env python3
"""Reconcile Workflow Registry schedules and terminalize orphaned runs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from hermes_cli import workflow_registry


APPLY_TOKEN = "RECONCILE-WORKFLOW-REGISTRY"


def load_live_jobs(profiles_root: Path) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for profile in sorted(path for path in profiles_root.iterdir() if path.is_dir()):
        path = profile / "cron" / "jobs.json"
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for job in payload.get("jobs") or []:
            jobs.append({
                "profile": profile.name,
                "cron_job_id": str(job.get("id") or ""),
                "workflow_id": str(job.get("workflow_id") or ""),
                "enabled": bool(job.get("enabled", True)),
            })
    return jobs


def reconcile(
    database: Path,
    profiles_root: Path,
    *,
    apply: bool,
    backup: Path | None = None,
    max_run_age_seconds: int = 6 * 3600,
) -> dict[str, Any]:
    jobs = load_live_jobs(profiles_root)
    if apply:
        if backup is None:
            raise ValueError("apply requires a backup destination")
        with workflow_registry.connect_closing(database) as source:
            workflow_registry.backup_db(source, backup)
        os.chmod(backup, 0o600)
        target = database
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="workflow-registry-reconcile-")
        target = Path(temp_dir.name) / "workflow_registry.db"
        with workflow_registry.connect_closing(database) as source:
            workflow_registry.backup_db(source, target)

    with workflow_registry.connect_closing(target) as conn:
        schedule = workflow_registry.reconcile_schedule_links(conn, jobs)
        runs = workflow_registry.settle_orphaned_runs(
            conn, max_age_seconds=max_run_age_seconds
        )
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if not apply:
        temp_dir.cleanup()
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "applied": apply,
        "integrity_check": integrity,
        "live_jobs": len(jobs),
        "schedule_changes": schedule,
        "settled_runs": runs,
        "backup": str(backup) if backup else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--profiles-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--max-run-age-seconds", type=int, default=6 * 3600)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    if args.apply and args.confirm != APPLY_TOKEN:
        parser.error(f"--apply requires --confirm {APPLY_TOKEN}")
    result = reconcile(
        args.database,
        args.profiles_root,
        apply=args.apply,
        backup=args.backup,
        max_run_age_seconds=args.max_run_age_seconds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(args.output, 0o600)
    print(json.dumps({
        "applied": result["applied"],
        "integrity_check": result["integrity_check"],
        "schedule_change_counts": {key: len(value) for key, value in result["schedule_changes"].items()},
        "settled_runs": len(result["settled_runs"]),
    }, indent=2, sort_keys=True))
    return 0 if result["integrity_check"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
