#!/usr/bin/env python3
"""Stage canonical runbook links for enabled Cron jobs missing Registry identity."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from hermes_cli.runbook_schema import render_frontmatter, split_frontmatter
from hermes_cli.workforce_org import load_organization


RUNBOOK_REF = re.compile(r"canonical runbook\s+`([^`]+)`", re.I)


def stage(profiles_root: Path, organization: Path, runbooks_root: Path, output: Path) -> dict[str, Any]:
    org = load_organization(organization)
    operational = {Path(str(item.profile_path)).name for item in org.operational_agents(include_planned=False)}
    changes = []
    unresolved = []
    by_slug: dict[str, dict[str, Any]] = {}
    for profile in sorted(operational):
        path = profiles_root / profile / "cron/jobs.json"
        if not path.is_file():
            continue
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        jobs = raw.get("jobs", []) if isinstance(raw, dict) else raw
        for job in jobs if isinstance(jobs, list) else []:
            if not isinstance(job, dict) or job.get("enabled") is False:
                continue
            if job.get("workflow_id") or job.get("workflow_slug") or job.get("runbook_slug"):
                continue
            job_id = str(job.get("id") or "")
            match = RUNBOOK_REF.search(str(job.get("prompt") or ""))
            if not match:
                unresolved.append({"profile": profile, "job_id": job_id, "reason": "no explicit canonical runbook reference"})
                continue
            slug = match.group(1).strip()
            source = runbooks_root / slug / "RUNBOOK.md"
            if not source.is_file():
                unresolved.append({"profile": profile, "job_id": job_id, "reason": f"referenced runbook is absent: {slug}"})
                continue
            entry = by_slug.setdefault(slug, {"source": source, "jobs": []})
            entry["jobs"].append((profile, job))

    candidates = output / "runbooks"
    for slug, entry in sorted(by_slug.items()):
        parsed = split_frontmatter(entry["source"].read_text(encoding="utf-8-sig"))
        metadata = dict(parsed.metadata)
        schedules = list(metadata.get("schedules") or [])
        steps = list(metadata.get("steps") or [])
        for profile, job in entry["jobs"]:
            job_id = str(job["id"])
            step_key = f"job_{job_id}"
            schedule_id = f"cron_{job_id}"
            if not any(str(step.get("step_key")) == step_key for step in steps):
                steps.append({
                    "step_key": step_key,
                    "name": str(job.get("name") or job_id),
                    "description": "Execute the preserved Hermes Cron contract.",
                    "executor_profile": profile,
                })
            if not any(str(item.get("id")) == schedule_id for item in schedules):
                schedule = job.get("schedule") or {}
                schedule_text = schedule.get("expr") if isinstance(schedule, dict) else str(schedule)
                schedules.append({
                    "id": schedule_id,
                    "name": str(job.get("name") or job_id),
                    "profile": profile,
                    "cron_job_id": job_id,
                    "schedule": str(schedule_text or job.get("schedule_display") or ""),
                    "enabled": True,
                    "step_key": step_key,
                })
            changes.append({
                "profile": profile,
                "job_id": job_id,
                "runbook_slug": slug,
                "workflow_id": metadata["id"],
                "workflow_step_key": step_key,
                "workflow_schedule_id": schedule_id,
                "job_patch_not_executed": {
                    "workflow_id": metadata["id"], "workflow_slug": slug,
                    "runbook_slug": slug, "workflow_step_key": step_key,
                    "workflow_schedule_id": schedule_id,
                },
            })
        metadata["steps"] = steps
        metadata["schedules"] = schedules
        rendered = render_frontmatter(metadata, parsed.body)
        target = candidates / slug / "RUNBOOK.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        target.chmod(0o600)
        for change in changes:
            if change["runbook_slug"] == slug:
                change["source_runbook"] = str(entry["source"])
                change["source_sha256"] = hashlib.sha256(entry["source"].read_bytes()).hexdigest()
                change["candidate_runbook"] = str(target)
                change["candidate_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    report = {
        "schema_version": 1,
        "mutation_performed": False,
        "linked_candidates": len(changes),
        "unresolved": unresolved,
        "valid": not unresolved,
        "changes": changes,
    }
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest.chmod(0o600)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-root", type=Path, required=True)
    parser.add_argument("--organization", type=Path, required=True)
    parser.add_argument("--runbooks-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = stage(args.profiles_root, args.organization, args.runbooks_root, args.output)
    print(json.dumps({key: report[key] for key in ("valid", "linked_candidates", "unresolved", "mutation_performed")}, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
