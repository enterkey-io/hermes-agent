#!/usr/bin/env python3
"""Redirect migrated Grace cron prompts to canonical Hermes runbook slugs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.workflow_runtime import _cron_store_for_profile


REPLACEMENTS = {
    "019c9f963374": (
        "The canonical readable runbook is Evernote note `adf78d86-c842-4dc8-99c8-7093a3151b2d` (`[grace]: Morning Message Runbook`).",
        "The canonical procedure is Hermes runbook `grace-morning-message`; Evernote note `adf78d86-c842-4dc8-99c8-7093a3151b2d` is provenance only.",
    ),
    "e24182c7ae77": (
        "The canonical readable runbook is Evernote note `adf78d86-c842-4dc8-99c8-7093a3151b2d` (`[grace]: Morning Message Runbook`).",
        "The canonical procedure is Hermes runbook `grace-morning-message`; Evernote note `adf78d86-c842-4dc8-99c8-7093a3151b2d` is provenance only.",
    ),
    "537d8032bdaf": (
        "follow `/home/elliott/obsidian/Atlas/Resources/runbooks/grace/heartbeat.md`.",
        "follow canonical Hermes runbook `grace-heartbeat`.",
    ),
    "9e0af1442da4": (
        "follow `/home/elliott/obsidian/Atlas/Resources/runbooks/grace/heartbeat.md`.",
        "follow canonical Hermes runbook `grace-heartbeat`.",
    ),
    "b8a785e53bbe": (
        "Read Evernote runbook `a19d733d-be20-4c24-a5f1-69eeb9280f21`; only its final Current authority and Plaud transport-resilience sections control this run.",
        "Read canonical Hermes runbook `grace-meeting-transcript-processing`; its current Plaud contract controls this run. Evernote note `a19d733d-be20-4c24-a5f1-69eeb9280f21` is provenance only.",
    ),
    "9378c72794d9": (
        "Read Evernote runbook `a19d733d-be20-4c24-a5f1-69eeb9280f21`; only its final Current authority and Plaud transport-resilience sections control this run.",
        "Read canonical Hermes runbook `grace-meeting-transcript-processing`; its current Plaud contract controls this run. Evernote note `a19d733d-be20-4c24-a5f1-69eeb9280f21` is provenance only.",
    ),
}


def main() -> int:
    jobs_path = Path("/home/elliott/.hermes/profiles/grace/cron/jobs.json")
    raw = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs = raw.get("jobs", raw)
    indexed = {str(job["id"]): job for job in jobs}
    updates: dict[str, dict[str, object]] = {}
    for job_id, (old, new) in REPLACEMENTS.items():
        prompt = str(indexed[job_id].get("prompt") or "")
        payload: dict[str, object] = {}
        if new not in prompt:
            if prompt.count(old) != 1:
                raise RuntimeError(f"expected one authority reference in {job_id}")
            payload["prompt"] = prompt.replace(old, new)
        toolsets = indexed[job_id].get("enabled_toolsets")
        if isinstance(toolsets, list) and "runbook" not in toolsets:
            payload["enabled_toolsets"] = [*toolsets, "runbook"]
        if payload:
            updates[job_id] = payload
    if not updates:
        print(json.dumps({"updated": 0, "jobs": []}))
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = Path("/home/elliott/.hermes/backups") / f"runbook-authority-redirect-{stamp}" / "grace-jobs.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(jobs_path, backup)
    with _cron_store_for_profile("grace"):
        from cron import jobs as cron_jobs

        for job_id, payload in updates.items():
            if cron_jobs.update_job(job_id, payload) is None:
                raise RuntimeError(f"job disappeared during update: {job_id}")
    print(json.dumps({"updated": len(updates), "jobs": sorted(updates), "backup": str(backup)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
