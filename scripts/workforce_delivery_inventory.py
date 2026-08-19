#!/usr/bin/env python3
"""Build a secret-safe migration manifest for operational Cron delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

import yaml

from hermes_cli.workforce_org import load_organization


LEGACY_PATTERNS = {
    "telegram": re.compile(r"telegram", re.I),
    "matrix": re.compile(r"matrix", re.I),
    "photon": re.compile(r"photon", re.I),
    "direct_hermes_send": re.compile(r"\bhermes\s+send\b", re.I),
    "direct_platform_send": re.compile(
        r"send_(?:telegram|matrix|photon)|api\.telegram\.org|matrix.*send", re.I
    ),
}
PAPERCLIP_PATTERN = re.compile(r"\bpaperclip\b", re.I)
PAPERCLIP_ARCHIVE_MARKERS = (
    "do not",
    "never",
    "retired",
    "archive-only",
    "archive only",
    "backup-only",
    "backup only",
    "historical",
    "provenance",
)


def _strings(value: Any, prefix: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield prefix, value
    elif isinstance(value, dict):
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from _strings(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{prefix}[{index}]")


def _redact_destination(value: Any) -> str:
    text = str(value or "missing")
    platform = text.split(":", 1)[0].casefold()
    if platform in {"telegram", "matrix", "photon", "buzz"} and ":" in text:
        return f"{platform}:<redacted-target>"
    return text


def _hidden_routes(job: dict[str, Any], profile_home: Path) -> list[dict[str, str]]:
    findings: set[tuple[str, str]] = set()
    for field, value in _strings(job):
        if field == "deliver" or field.startswith("origin."):
            continue
        for kind, pattern in LEGACY_PATTERNS.items():
            if pattern.search(value):
                findings.add((kind, field))
    script_name = str(job.get("script") or "").strip()
    if script_name:
        candidates = [
            profile_home / "scripts" / script_name,
            profile_home.parent.parent / "scripts" / script_name,
        ]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                content = candidate.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            for kind, pattern in LEGACY_PATTERNS.items():
                if pattern.search(content):
                    findings.add((kind, "script"))
    return [{"kind": kind, "source": source} for kind, source in sorted(findings)]


def _origin_markers(job: dict[str, Any]) -> list[str]:
    value = str((job.get("origin") or {}).get("platform") or "").casefold()
    return [value] if value in {"telegram", "matrix", "photon", "buzz"} else []


def _paperclip_evidence(
    job: dict[str, Any], profile_home: Path
) -> tuple[list[dict[str, str]], str]:
    mentions: set[tuple[str, bool]] = set()
    for field, value in _strings(job):
        for line in value.splitlines():
            if not PAPERCLIP_PATTERN.search(line):
                continue
            normalized = line.casefold()
            archive_only = any(marker in normalized for marker in PAPERCLIP_ARCHIVE_MARKERS)
            mentions.add((field, archive_only))
    script_name = str(job.get("script") or "").strip()
    if script_name:
        candidates = [
            profile_home / "scripts" / script_name,
            profile_home.parent.parent / "scripts" / script_name,
        ]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                lines = candidate.read_text(
                    encoding="utf-8-sig", errors="replace"
                ).splitlines()
            except OSError:
                continue
            for line in lines:
                if PAPERCLIP_PATTERN.search(line):
                    normalized = line.casefold()
                    archive_only = any(
                        marker in normalized for marker in PAPERCLIP_ARCHIVE_MARKERS
                    )
                    mentions.add(("script", archive_only))
    evidence = [
        {"source": source, "archive_only_context": archive_only}
        for source, archive_only in sorted(mentions)
    ]
    if any(not archive_only for _source, archive_only in mentions):
        disposition = "remove-active-paperclip-route"
    elif mentions:
        disposition = "archive-only prohibition/provenance; no active route detected"
    else:
        disposition = "historical-lookup-only; no active route detected"
    return evidence, disposition


def _command(profile_home: Path, job_id: str, destination: str) -> str:
    return (
        f"HERMES_HOME={profile_home} hermes cron edit {job_id} "
        f"--deliver '{destination}'"
    )


def build_manifest(
    profiles_root: Path,
    organization_path: Path,
    policy_path: Path,
    topology_path: Path,
) -> dict[str, Any]:
    org = load_organization(organization_path)
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8-sig")) or {}
    topology = yaml.safe_load(topology_path.read_text(encoding="utf-8-sig")) or {}
    room_names = {str(room.get("name")) for room in topology.get("rooms", [])}
    defaults = policy.get("profile_defaults") or {}
    overrides = policy.get("job_overrides") or {}
    operational_profiles = {
        Path(item.profile_path).name: item
        for item in org.operational_agents(include_planned=False)
        if item.profile_path
    }
    jobs: list[dict[str, Any]] = []
    seen_override_keys: set[str] = set()
    for profile, agent in sorted(operational_profiles.items()):
        jobs_path = profiles_root / profile / "cron" / "jobs.json"
        if not jobs_path.is_file():
            continue
        raw = json.loads(jobs_path.read_text(encoding="utf-8-sig"))
        rows = raw.get("jobs", []) if isinstance(raw, dict) else raw
        for job in rows if isinstance(rows, list) else []:
            if not isinstance(job, dict) or job.get("enabled") is False:
                continue
            job_id = str(job.get("id") or "")
            key = f"{profile}/{job_id}"
            rule = dict(defaults.get(profile) or {})
            if key in overrides:
                rule.update(overrides[key] or {})
                seen_override_keys.add(key)
            mode = str(rule.get("mode") or "unclassified")
            room = str(rule.get("room") or "") or None
            if mode != "team":
                room = None
            failure_room = str(rule.get("failure_room") or room or "") or None
            quiet_success = bool(rule.get("quiet_success", True))
            if mode == "team" and room not in room_names:
                raise ValueError(f"{key}: unknown intended Buzz room {room!r}")
            if failure_room and failure_room not in room_names:
                raise ValueError(f"{key}: unknown failure Buzz room {failure_room!r}")
            current_raw = str(job.get("deliver") or "missing")
            current_platform = current_raw.split(":", 1)[0].casefold()
            if mode == "team":
                intended = f"buzz:<ROOM_UUID:{room}>"
                staged = (
                    None
                    if current_platform == "buzz"
                    else _command(profiles_root / profile, job_id, intended)
                )
            elif mode == "local-only":
                intended = "local"
                staged = None if current_raw == "local" else _command(
                    profiles_root / profile, job_id, "local"
                )
            elif mode == "private-personal":
                intended = _redact_destination(current_raw)
                staged = None
            else:
                intended = None
                staged = None
            hidden = _hidden_routes(job, profiles_root / profile)
            paperclip_mentions, paperclip_disposition = _paperclip_evidence(
                job, profiles_root / profile
            )
            disallowed = mode == "team" and (
                current_platform in {"telegram", "matrix", "photon", "origin", "missing"}
                or bool(hidden)
            )
            jobs.append(
                {
                    "profile": profile,
                    "owner_agent": agent.agent,
                    "job_id": job_id,
                    "name_sha256": hashlib.sha256(
                        str(job.get("name") or "").encode("utf-8")
                    ).hexdigest(),
                    "workflow_id": job.get("workflow_id"),
                    "workflow_slug": job.get("workflow_slug") or job.get("runbook_slug"),
                    "current_destination": _redact_destination(current_raw),
                    "destination_verification": (
                        "compare the redacted Buzz target with the approved room map"
                        if mode == "team" and current_platform == "buzz"
                        else None
                    ),
                    "classification": mode,
                    "privacy": "private-personal" if mode == "private-personal" else "operational",
                    "audience": rule.get("audience"),
                    "intended_destination": intended,
                    "intended_room": room,
                    "failure_room": failure_room,
                    "quiet_success": quiet_success,
                    "exception_reason": rule.get("reason"),
                    "hidden_delivery_paths": hidden,
                    "direct_send_fallbacks": [
                        item for item in hidden
                        if item["kind"] in {"direct_hermes_send", "direct_platform_send"}
                    ],
                    "origin_provenance_markers": _origin_markers(job),
                    "registry_cron_status": (
                        "registered"
                        if job.get("workflow_id") or job.get("workflow_slug") or job.get("runbook_slug")
                        else "unregistered"
                    ),
                    "registry_cron_mismatch": not bool(
                        job.get("workflow_id") or job.get("workflow_slug") or job.get("runbook_slug")
                    ),
                    "validation_blocked_until_registry_reconciled": not bool(
                        job.get("workflow_id") or job.get("workflow_slug") or job.get("runbook_slug")
                    ),
                    "paperclip_mentions": paperclip_mentions,
                    "legacy_paperclip_disposition": paperclip_disposition,
                    "migration_required": bool(staged),
                    "validation_blocked_until_migrated": disallowed,
                    "staged_change_not_executed": staged,
                    "rollback_source": (
                        "restore original deliver field from the verified pre-cutover profile backup"
                        if staged else None
                    ),
                }
            )
    unused_overrides = sorted(set(overrides) - seen_override_keys)
    unclassified = [f"{row['profile']}/{row['job_id']}" for row in jobs if row["classification"] == "unclassified"]
    return {
        "schema_version": 1,
        "mutation_performed": False,
        "enabled_operational_jobs": len(jobs),
        "jobs": jobs,
        "summary": {
            "team_operational": sum(row["classification"] == "team" for row in jobs),
            "local_only": sum(row["classification"] == "local-only" for row in jobs),
            "private_personal": sum(row["classification"] == "private-personal" for row in jobs),
            "migration_required": sum(row["migration_required"] for row in jobs),
            "blocked_by_legacy_or_hidden_delivery": sum(
                row["validation_blocked_until_migrated"] for row in jobs
            ),
            "registry_cron_mismatches": sum(row["registry_cron_mismatch"] for row in jobs),
            "direct_send_fallbacks": sum(bool(row["direct_send_fallbacks"]) for row in jobs),
            "active_paperclip_routes": sum(
                row["legacy_paperclip_disposition"] == "remove-active-paperclip-route"
                for row in jobs
            ),
            "unclassified": len(unclassified),
        },
        "unclassified": unclassified,
        "unused_policy_overrides": unused_overrides,
        "valid": not unclassified,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-root", type=Path, required=True)
    parser.add_argument("--organization", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = build_manifest(
            args.profiles_root, args.organization, args.policy, args.topology
        )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        report = {"valid": False, "mutation_performed": False, "error": str(exc)}
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        args.output.chmod(0o600)
    print(rendered, end="")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
