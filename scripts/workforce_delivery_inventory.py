#!/usr/bin/env python3
"""Build a secret-safe migration manifest for operational Cron delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable
from uuid import UUID

import yaml

from hermes_cli.runbook_schema import split_frontmatter
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
PAPERCLIP_ARCHIVE_PATTERNS = (
    re.compile(r"\b(?:do not|never)\b[^.;:\n]{0,80}\bpaperclip\b", re.I),
    re.compile(
        r"\bpaperclip\b[^.;:\n]{0,80}"
        r"\b(?:retired|archive-only|archive only|backup-only|backup only|historical|provenance)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:retired|archive-only|archive only|backup-only|backup only|historical|provenance)\b"
        r"[^.;:\n]{0,80}\bpaperclip\b",
        re.I,
    ),
)


def _paperclip_is_archive_only(line: str) -> bool:
    """Return true only when an archive marker qualifies Paperclip itself."""
    return any(pattern.search(line) for pattern in PAPERCLIP_ARCHIVE_PATTERNS)


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


def load_room_map(path: Path) -> dict[str, str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(raw, dict):
        raise ValueError("room map must be a mapping of room names to UUIDs")
    result: dict[str, str] = {}
    for key, raw_value in raw.items():
        name = str(key)
        value = str(raw_value)
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError):
            raise ValueError(f"{name}: room id is not a UUID")
        canonical = str(parsed)
        if canonical != value.casefold():
            raise ValueError(f"{name}: room id is not a canonical UUID")
        result[name] = canonical
    return result


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
            archive_only = _paperclip_is_archive_only(line)
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
                    archive_only = _paperclip_is_archive_only(line)
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


def _cron_expression(job: dict[str, Any]) -> str | None:
    schedule = job.get("schedule")
    if isinstance(schedule, dict):
        value = schedule.get("expr") or schedule.get("display")
    else:
        value = schedule
    normalized = str(value or "").strip()
    return normalized or None


def _runbook_schedule_contracts(
    runbooks_root: Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    by_id: dict[str, list[dict[str, Any]]] = {}
    by_slug: dict[str, list[dict[str, Any]]] = {}
    if not runbooks_root.is_dir():
        return by_id, by_slug
    for path in sorted(runbooks_root.glob("*/RUNBOOK.md")):
        parsed = split_frontmatter(path.read_text(encoding="utf-8-sig"))
        metadata = parsed.metadata
        contract = {
            "id": str(metadata.get("id") or ""),
            "slug": str(metadata.get("slug") or ""),
            "schedules": list(metadata.get("schedules") or []),
        }
        if contract["id"]:
            by_id.setdefault(contract["id"], []).append(contract)
        if contract["slug"]:
            by_slug.setdefault(contract["slug"], []).append(contract)
    return by_id, by_slug


def _registry_cron_mismatch_reasons(
    job: dict[str, Any],
    profile: str,
    contracts_by_id: dict[str, list[dict[str, Any]]],
    contracts_by_slug: dict[str, list[dict[str, Any]]],
) -> list[str]:
    workflow_id = str(job.get("workflow_id") or "").strip()
    workflow_slug = str(job.get("workflow_slug") or job.get("runbook_slug") or "").strip()
    if not workflow_id and not workflow_slug:
        return ["cron job has no workflow/runbook identity"]

    candidates: list[dict[str, Any]] = []
    if workflow_id:
        candidates.extend(contracts_by_id.get(workflow_id, []))
    if workflow_slug:
        candidates.extend(contracts_by_slug.get(workflow_slug, []))
    unique = {(item["id"], item["slug"]): item for item in candidates}
    if len(unique) != 1:
        return [
            "linked canonical runbook is missing"
            if not unique
            else "workflow identity resolves to multiple canonical runbooks"
        ]
    contract = next(iter(unique.values()))
    if workflow_id and contract["id"] != workflow_id:
        return ["workflow_id does not match canonical runbook"]
    if workflow_slug and contract["slug"] != workflow_slug:
        return ["workflow_slug does not match canonical runbook"]

    job_id = str(job.get("id") or "")
    schedules = [
        item for item in contract["schedules"]
        if isinstance(item, dict)
        and str(item.get("cron_job_id") or "") == job_id
        and str(item.get("profile") or "") == profile
    ]
    if not schedules:
        job_name = str(job.get("name") or "")
        schedules = [
            item for item in contract["schedules"]
            if isinstance(item, dict)
            and not str(item.get("cron_job_id") or "")
            and str(item.get("profile") or "") == profile
            and str(item.get("name") or "") == job_name
        ]
    if len(schedules) != 1:
        return [
            "canonical runbook has no matching profile/cron_job_id schedule"
            if not schedules
            else "canonical runbook has duplicate profile/cron_job_id schedules"
        ]
    runbook_schedule = schedules[0]
    reasons: list[str] = []
    if str(runbook_schedule.get("schedule") or "").strip() != _cron_expression(job):
        reasons.append("Cron expression differs from canonical runbook")
    if bool(runbook_schedule.get("enabled", True)) != bool(job.get("enabled", True)):
        reasons.append("enabled state differs from canonical runbook")
    job_timezone = str(job.get("timezone") or "").strip()
    runbook_timezone = str(runbook_schedule.get("timezone") or "").strip()
    if job_timezone and runbook_timezone and job_timezone != runbook_timezone:
        reasons.append("timezone differs from canonical runbook")
    return reasons


def build_manifest(
    profiles_root: Path,
    organization_path: Path,
    policy_path: Path,
    topology_path: Path,
    room_map: dict[str, str] | None = None,
    runbooks_root: Path | None = None,
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
    contracts_by_id: dict[str, list[dict[str, Any]]] = {}
    contracts_by_slug: dict[str, list[dict[str, Any]]] = {}
    if runbooks_root is not None:
        contracts_by_id, contracts_by_slug = _runbook_schedule_contracts(runbooks_root)
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
                mapped_room = room_map.get(str(room)) if room_map is not None else None
                expected_raw = f"buzz:{mapped_room}" if mapped_room else None
                destination_verified = expected_raw is not None
                destination_matches = destination_verified and current_raw == expected_raw
                staged = None if destination_matches else _command(
                    profiles_root / profile, job_id, intended
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
            registry_mismatch_reasons = (
                _registry_cron_mismatch_reasons(
                    job, profile, contracts_by_id, contracts_by_slug
                )
                if runbooks_root is not None
                else (
                    []
                    if job.get("workflow_id") or job.get("workflow_slug") or job.get("runbook_slug")
                    else ["cron job has no workflow/runbook identity"]
                )
            )
            disallowed = mode == "team" and (
                current_platform in {"telegram", "matrix", "photon", "origin", "missing"}
                or bool(hidden)
                or not destination_verified
                or not destination_matches
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
                        (
                            "verified against the approved room map"
                            if destination_matches
                            else "destination does not match the approved room map"
                        )
                        if mode == "team" and destination_verified
                        else (
                            "blocked pending comparison with an approved room map"
                            if mode == "team"
                            else None
                        )
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
                    "registry_cron_mismatch": bool(registry_mismatch_reasons),
                    "registry_cron_mismatch_reasons": registry_mismatch_reasons,
                    "validation_blocked_until_registry_reconciled": bool(
                        registry_mismatch_reasons
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
    registry_mismatches = [
        f"{row['profile']}/{row['job_id']}"
        for row in jobs
        if row["registry_cron_mismatch"]
    ]
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
        "registry_cron_mismatches": registry_mismatches,
        "unused_policy_overrides": unused_overrides,
        "valid": not unclassified and (
            runbooks_root is None or not registry_mismatches
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-root", type=Path, required=True)
    parser.add_argument("--organization", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--room-map", type=Path)
    parser.add_argument(
        "--runbooks-root",
        type=Path,
        help="Canonical runbooks root; when supplied, validate exact schedule parity",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = build_manifest(
            args.profiles_root,
            args.organization,
            args.policy,
            args.topology,
            load_room_map(args.room_map) if args.room_map else None,
            args.runbooks_root,
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
