#!/usr/bin/env python3
"""Produce a secret-safe factual inventory for workforce rollout evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any

import yaml

from hermes_cli.runbook_schema import split_frontmatter
from hermes_cli.workforce_org import load_organization


def _run(args: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def _version(args: list[str]) -> str | None:
    code, output = _run(args)
    return output.splitlines()[0] if code == 0 and output else None


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _db_inventory(path: Path, queries: dict[str, str]) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        results = {}
        for name, query in queries.items():
            rows = conn.execute(query).fetchall()
            results[name] = [dict(row) for row in rows]
        return {
            "exists": True,
            "mode": path.stat().st_mode & 0o777,
            "size": path.stat().st_size,
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "tables": tables,
            "queries": results,
        }
    finally:
        conn.close()


def _active_crontab() -> dict[str, Any]:
    code, output = _run(["crontab", "-l"])
    lines = [] if code else [
        line.strip() for line in output.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line.strip())
    ]
    markers = {name: 0 for name in ("telegram", "matrix", "photon", "buzz", "origin", "paperclip", "hermes send")}
    records = []
    for index, line in enumerate(lines, 1):
        lower = line.casefold()
        found = [name for name in markers if name in lower]
        for name in found:
            markers[name] += 1
        records.append({
            "ordinal": index,
            "sha256": hashlib.sha256(line.encode()).hexdigest(),
            "delivery_markers": found,
        })
    return {"active_lines": len(lines), "marker_counts": markers, "records": records}


def _systemd_inventory() -> dict[str, Any]:
    _, timers = _run(["systemctl", "--user", "list-timers", "--all", "--no-legend"])
    _, services = _run(["systemctl", "--user", "list-units", "--type=service", "--all", "--no-legend"])
    relevant = ("hermes", "buzz", "paperclip", "nanoclaw")
    def names(output: str) -> list[str]:
        found: set[str] = set()
        for line in output.splitlines():
            if not any(token in line.casefold() for token in relevant):
                continue
            for token in line.split():
                if token.endswith((".timer", ".service")):
                    found.add(token)
        return sorted(found)
    return {"timers": names(timers), "services": names(services)}


def build_inventory(
    *,
    repo: Path,
    main_checkout: Path,
    profiles_root: Path,
    organization: Path,
    workflow_db: Path,
    kanban_db: Path,
    runbook_roots: list[Path],
) -> dict[str, Any]:
    org = load_organization(organization)
    profiles = []
    for path in sorted(item for item in profiles_root.iterdir() if item.is_dir()):
        jobs_path = path / "cron" / "jobs.json"
        enabled_jobs = []
        if jobs_path.is_file():
            raw = json.loads(jobs_path.read_text(encoding="utf-8-sig"))
            rows = raw.get("jobs", []) if isinstance(raw, dict) else raw
            enabled_jobs = [job for job in rows if isinstance(job, dict) and job.get("enabled") is not False]
        config_path = path / "config.yaml"
        toolsets: list[str] = []
        if config_path.is_file():
            config = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
            value = config.get("toolsets", []) if isinstance(config, dict) else []
            toolsets = sorted(str(item) for item in value) if isinstance(value, list) else []
        instruction = path / "AGENTS.md"
        try:
            org_item = org.from_profile_path(path.name)
            org_status = org_item.status
            operational = org_item.operational
        except Exception:
            org_status = "unmapped"
            operational = False
        profiles.append({
            "profile": path.name,
            "organization_status": org_status,
            "operational": operational,
            "agents_sha256": _sha(instruction) if instruction.is_file() else None,
            "agents_mtime_ns": instruction.stat().st_mtime_ns if instruction.is_file() else None,
            "enabled_cron_jobs": len(enabled_jobs),
            "delivery_platforms": sorted({
                str(job.get("deliver") or "missing").split(":", 1)[0]
                for job in enabled_jobs
            }),
            "toolsets": toolsets,
        })
    runbooks = []
    seen: set[Path] = set()
    for root in runbook_roots:
        for path in sorted(root.rglob("RUNBOOK.md")) if root.is_dir() else []:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                parsed = split_frontmatter(path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            runbooks.append({
                "slug": parsed.metadata.get("slug"),
                "owner_profile": parsed.metadata.get("owner_profile"),
                "status": parsed.metadata.get("status"),
                "schedule_count": len(parsed.metadata.get("schedules") or []),
            })
    _, worktree_commit = _run(["git", "rev-parse", "HEAD"], cwd=repo)
    _, main_commit = _run(["git", "rev-parse", "HEAD"], cwd=main_checkout)
    _, main_status = _run(["git", "status", "--short"], cwd=main_checkout)
    return {
        "schema_version": 1,
        "mutation_performed": False,
        "repositories": {
            "worktree": str(repo),
            "worktree_commit": worktree_commit,
            "main_checkout": str(main_checkout),
            "main_commit": main_commit,
            "main_dirty_paths": main_status.splitlines() if main_status else [],
        },
        "versions": {
            "python": _version(["python3", "--version"]),
            "node": _version(["node", "--version"]),
            "codex": _version(["codex", "--version"]),
            "hermes": _version(["hermes", "--version"]),
            "buzz": {
                "reported_version": _version(["buzz", "--version"]),
                "binary_sha256": _sha(Path("/home/elliott/.local/bin/buzz")),
            },
        },
        "context_precedence": {
            "implementation": "agent/prompt_builder.py:2383",
            "order": [".hermes.md/HERMES.md", "AGENTS.md chain", "CLAUDE.md", ".cursorrules"],
            "soul": "HERMES_HOME/SOUL.md is independent identity context",
            "workforce_inheritance": "managed AGENTS.md block per operational profile",
        },
        "profiles": profiles,
        "profile_count": len(profiles),
        "operational_profile_count": len(org.operational_agents(include_planned=False)),
        "planned_profiles": [item.agent for item in org.operational_agents() if item.status == "planned"],
        "workflow_registry": _db_inventory(
            workflow_db,
            {
                "definitions_by_status": "SELECT status, COUNT(*) count FROM workflow_definitions GROUP BY status ORDER BY status",
                "definitions_by_runtime": "SELECT runtime_kind, COUNT(*) count FROM workflow_definitions GROUP BY runtime_kind ORDER BY runtime_kind",
                "schedule_count": "SELECT COUNT(*) count FROM workflow_schedules",
            },
        ),
        "kanban": _db_inventory(
            kanban_db,
            {"tasks_by_status": "SELECT status, COUNT(*) count FROM tasks GROUP BY status ORDER BY status"},
        ),
        "runbooks": runbooks,
        "runbook_count": len(runbooks),
        "host_crontab": _active_crontab(),
        "systemd": _systemd_inventory(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--main-checkout", type=Path, required=True)
    parser.add_argument("--profiles-root", type=Path, required=True)
    parser.add_argument("--organization", type=Path, required=True)
    parser.add_argument("--workflow-db", type=Path, required=True)
    parser.add_argument("--kanban-db", type=Path, required=True)
    parser.add_argument("--runbook-root", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = build_inventory(
        repo=args.repo,
        main_checkout=args.main_checkout,
        profiles_root=args.profiles_root,
        organization=args.organization,
        workflow_db=args.workflow_db,
        kanban_db=args.kanban_db,
        runbook_roots=args.runbook_root,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        args.output.chmod(0o600)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
