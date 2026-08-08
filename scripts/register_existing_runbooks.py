#!/usr/bin/env python3
"""Register retained Hermes schedules and reviewed Evernote runbooks.

The migration is deliberately metadata-only for existing cron jobs: it adds
workflow identity fields but preserves prompts, schedules, delivery, models,
scripts, enabled state, and workflow-status tracking behavior.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli import runbook_store
from hermes_cli.runbook_projection import project_runbook
from hermes_cli.workflow_runtime import link_existing_cron_job
from hermes_constants import get_default_hermes_root


DEFAULT_CLASSIFICATION = Path(
    "/home/elliott/.hermes/profiles/grace/migration/runbook-classification-20260730.json"
)
DEFAULT_EVERNOTE_LEDGER = Path(
    "/home/elliott/.hermes/profiles/grace/migration/evernote-historical-reference-20260729.json"
)

REVIEWED: dict[str, dict[str, Any]] = {
    "grace-heartbeat": {
        "title": "Grace Heartbeat",
        "purpose": "Decide whether a scheduled personal outreach is timely and grounded.",
        "owner": "grace",
        "source": "grace/heartbeat.md",
        "jobs": [("grace", "537d8032bdaf"), ("grace", "9e0af1442da4")],
    },
    "grace-morning-message": {
        "title": "Grace Morning Message",
        "purpose": "Prepare and deliver Elliott's daily morning message.",
        "owner": "grace",
        "source": "grace/morning-message.md",
        "jobs": [("grace", "019c9f963374"), ("grace", "e24182c7ae77")],
    },
    "grace-meeting-preparation": {
        "title": "Grace Meeting Preparation",
        "purpose": "Prepare, deliver, persist, and reconcile LIFT meeting briefs.",
        "owner": "grace",
        "source": "grace/meeting-prep.md",
        "jobs": [("grace", "be5404c1511b"), ("grace", "a666c92adcc1")],
    },
    "grace-meeting-transcript-processing": {
        "title": "Grace Meeting Transcript Processing",
        "purpose": "Collect Teams and Plaud transcripts and persist verified meeting notes.",
        "owner": "grace",
        "source": "grace/plaud-transcript-processing.md",
        "jobs": [
            ("grace", "b8a785e53bbe"),
            ("grace", "9378c72794d9"),
            ("grace", "59ea71ff521c"),
        ],
    },
    "margot-weekly-blog-post": {
        "title": "Margot Weekly Blog Post",
        "purpose": "Prepare and, when publication is enabled, publish Margot's weekly Ghost post.",
        "owner": "margot",
        "source": "margot/weekly-blog-post.md",
        "jobs": [("margot", "d0c36ca9f7bf")],
    },
    "shared-vt-signal-flow": {
        "title": "VT Signal Flow",
        "purpose": "Route VT signal email events through parsing, notification, and trade analysis.",
        "owner": "shared",
        "source": "shared/vt-signal-flow.md",
        "jobs": [],
        "runtime_kind": "external_cli",
    },
}


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "schedule"


def _workflow_id(slug: str) -> str:
    return "wf_migrated_" + hashlib.sha256(slug.encode()).hexdigest()[:20]


def _load_jobs(root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted((root / "profiles").glob("*/cron/jobs.json")):
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        jobs = raw.get("jobs", []) if isinstance(raw, dict) else raw
        for job in jobs if isinstance(jobs, list) else []:
            if isinstance(job, dict) and job.get("id"):
                result[(path.parent.parent.name, str(job["id"]))] = job
    return result


def _evernote_items(path: Path) -> dict[str, dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(item.get("source_key")): item
        for item in raw.get("items", [])
        if isinstance(item, dict) and item.get("source_key")
    }


def _schedule_text(job: dict[str, Any]) -> str:
    if job.get("schedule_display"):
        return str(job["schedule_display"])
    schedule = job.get("schedule")
    if isinstance(schedule, dict):
        return str(schedule.get("expr") or schedule.get("display") or schedule.get("every") or "")
    return str(schedule or "")


def _steps_and_schedules(
    jobs: list[tuple[str, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    steps: list[dict[str, Any]] = []
    schedules: list[dict[str, Any]] = []
    for profile, job in jobs:
        job_id = str(job["id"])
        step_key = "job_" + job_id
        steps.append(
            {
                "step_key": step_key,
                "name": str(job.get("name") or job_id),
                "description": "Execute the preserved Hermes Cron contract.",
                "executor_profile": profile,
            }
        )
        schedules.append(
            {
                "id": "cron_" + job_id,
                "name": str(job.get("name") or job_id),
                "profile": profile,
                "cron_job_id": job_id,
                "schedule": _schedule_text(job),
                "enabled": bool(job.get("enabled", True)),
                "step_key": step_key,
            }
        )
    return steps or [{"step_key": "external", "name": "Execute external workflow"}], schedules


def _prompt_body(title: str, jobs: list[tuple[str, dict[str, Any]]]) -> str:
    lines = [f"# {title}", "", "> Imported from the existing Hermes Cron authority without rewriting its instructions.", ""]
    for profile, job in jobs:
        lines.extend(
            [
                f"## {job.get('name') or job['id']}",
                "",
                f"Profile: `{profile}`  ",
                f"Cron job: `{job['id']}`",
                "",
                "~~~~text",
                str(job.get("prompt") or ""),
                "~~~~",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _metadata(
    *,
    slug: str,
    title: str,
    purpose: str,
    owner: str,
    jobs: list[tuple[str, dict[str, Any]]],
    related: dict[str, Any],
    runtime_kind: str = "hermes",
) -> dict[str, Any]:
    steps, schedules = _steps_and_schedules(jobs)
    enabled = any(job.get("enabled", True) for _, job in jobs) if jobs else True
    return {
        "id": _workflow_id(slug),
        "slug": slug,
        "title": title,
        "purpose": purpose,
        "owner_profile": owner,
        "status": "active" if enabled else "paused",
        "runtime": {"kind": runtime_kind, "ref": f"profile:{owner}"},
        "schedules": schedules,
        "steps": steps,
        "inputs": {},
        "outputs": {},
        "permitted_writes": ["Only writes already authorized by the preserved execution contract."],
        "approval_rules": {"policy": "Existing profile authority and Hermes smart approval policy apply."},
        "retry": {"max_attempts": 1},
        "timeout": {},
        "deduplication": {"strategy": "existing-scheduler-contract"},
        "related": related,
    }


def _copy_migration_catalog(
    root: Path,
    classification_path: Path,
    evernote_path: Path,
) -> Path:
    classification = json.loads(classification_path.read_text(encoding="utf-8"))
    evernote = json.loads(evernote_path.read_text(encoding="utf-8"))
    note_by_source = {
        item.get("source_key", "").removeprefix("runbook:"): item
        for item in evernote.get("items", [])
        if isinstance(item, dict)
    }
    candidates: list[dict[str, Any]] = []
    for disposition, paths in classification.get("dispositions", {}).items():
        for source in paths:
            note = note_by_source.get(source, {})
            effective = disposition
            if source == "shared/paperclip-task-standards.md":
                effective = "historical-superseded"
            candidates.append(
                {
                    "source": source,
                    "classification": effective,
                    "original_classification": disposition,
                    "title": note.get("title"),
                    "owner": note.get("runbook_owner"),
                    "evernote_note_id": note.get("evernote_note_id"),
                    "evernote_status": note.get("status"),
                    "source_sha256": (note.get("source_sha256") or [None])[0],
                }
            )
    catalog = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_classification": str(classification_path),
        "source_evernote_ledger": str(evernote_path),
        "candidates": sorted(candidates, key=lambda item: item["source"]),
    }
    target = root / "runbook-migrations" / "evernote-runbooks.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def migrate(
    *,
    root: Path,
    source_root: Path,
    classification_path: Path,
    evernote_path: Path,
    apply: bool,
) -> dict[str, Any]:
    all_jobs = _load_jobs(root)
    evernote = _evernote_items(evernote_path)
    existing_linked = {key for key, job in all_jobs.items() if job.get("workflow_id")}
    claimed: set[tuple[str, str]] = set()
    specs: list[tuple[dict[str, Any], str, list[tuple[str, dict[str, Any]]]]] = []

    for slug, spec in REVIEWED.items():
        jobs = [(profile, all_jobs[(profile, job_id)]) for profile, job_id in spec["jobs"]]
        claimed.update(spec["jobs"])
        source_rel = str(spec["source"])
        source = source_root / source_rel
        note = evernote.get("runbook:" + source_rel, {})
        related = {
            "migration": "evernote-reviewed-2026-08-07",
            "source_path": str(source),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "evernote_note_id": note.get("evernote_note_id"),
            "evernote_title": note.get("title"),
            "evernote_status": note.get("status"),
        }
        metadata = _metadata(
            slug=slug,
            title=spec["title"],
            purpose=spec["purpose"],
            owner=spec["owner"],
            jobs=jobs,
            related=related,
            runtime_kind=spec.get("runtime_kind", "hermes"),
        )
        specs.append((metadata, source.read_text(encoding="utf-8"), jobs))

    for (profile, job_id), job in sorted(all_jobs.items()):
        if (profile, job_id) in claimed or (profile, job_id) in existing_linked:
            continue
        if not job.get("enabled", True):
            continue
        slug = _slug(f"{profile}-{job.get('name') or job_id}")
        jobs = [(profile, job)]
        metadata = _metadata(
            slug=slug,
            title=str(job.get("name") or job_id),
            purpose=f"Canonical registry record for existing Hermes Cron job {profile}/{job_id}.",
            owner=profile,
            jobs=jobs,
            related={"migration": "existing-hermes-cron-2026-08-07", "cron_job_id": job_id},
        )
        specs.append((metadata, _prompt_body(metadata["title"], jobs), jobs))

    summary = {
        "apply": apply,
        "runbooks": len(specs),
        "schedule_links": sum(len(jobs) for _, _, jobs in specs),
        "existing_linked": len(existing_linked),
        "slugs": [metadata["slug"] for metadata, _, _ in specs],
    }
    if not apply:
        return summary

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = root / "backups" / f"runbook-linkage-{stamp}"
    for profile in sorted({profile for _, _, jobs in specs for profile, _ in jobs}):
        source = root / "profiles" / profile / "cron" / "jobs.json"
        target = backup_root / profile / "jobs.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    for metadata, body, jobs in specs:
        record = runbook_store.save_runbook(
            metadata,
            body,
            approved_by="system-admin-migration-2026-08-07",
        )
        project_runbook(record)
        schedule_by_job = {
            str(schedule["cron_job_id"]): schedule for schedule in metadata["schedules"]
        }
        for profile, job in jobs:
            schedule = schedule_by_job[str(job["id"])]
            link_existing_cron_job(
                metadata["slug"],
                profile=profile,
                cron_job_id=str(job["id"]),
                schedule_id=str(schedule["id"]),
                step_key=str(schedule["step_key"]),
            )
    summary["backup_root"] = str(backup_root)
    summary["migration_catalog"] = str(
        _copy_migration_catalog(root, classification_path, evernote_path)
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--hermes-root", type=Path, default=get_default_hermes_root())
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path("/home/elliott/obsidian/Atlas/Resources/runbooks"),
    )
    parser.add_argument("--classification", type=Path, default=DEFAULT_CLASSIFICATION)
    parser.add_argument("--evernote-ledger", type=Path, default=DEFAULT_EVERNOTE_LEDGER)
    args = parser.parse_args()
    result = migrate(
        root=args.hermes_root,
        source_root=args.source_root,
        classification_path=args.classification,
        evernote_path=args.evernote_ledger,
        apply=args.apply,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
